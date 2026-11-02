"""Cold proof of one V4 EARLY/END returned paid/incomplete checkpoint."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_checkpoint_end_input import decode_checkpoint_end_input
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_RESULT_SCHEMA_ERROR,
    ComposerOperationError,
    composer_operation_result_hash,
)
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, _effect_snapshot, load_snapshot
from elspeth.web.sessions.continuation_checkpoint_paid_tail import (
    build_checkpoint_paid_overflow_tail_batch,
)
from elspeth.web.sessions.continuation_checkpoint_terminal_hold import prepare_checkpoint_terminal_hold_plan
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
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class VerifiedCheckpointPaidOverflowTerminal:
    source: ProviderTerminalHoldSource
    receipt: PaidOverflowReceipt
    terminal: StoredContinuationSnapshot
    provider_attempt_id: str
    hold_state: str
    phase: str
    outer_effect_key: str


def checkpoint_paid_overflow_error(request_id: str | None, cost: PaidOverflowCost) -> ComposerOperationError:
    if type(cost) is not PaidOverflowCost:
        raise TypeError("checkpoint paid overflow public error needs its bounded original cost")
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


def _stored_hold_source(conn: Connection, sid: str, operation: str, effect_key: str) -> ProviderTerminalHoldSource | None:
    row = conn.execute(
        select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation, holds.c.effect_key == effect_key)
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
    value = read_bounded_retained_chunk(
        conn, session_id=source.session_id, operation_id=source.operation_id, key=key, allowance=source.allowance_bytes
    )
    if value is None or value.codec != codec:
        raise AuditIntegrityError("checkpoint paid overflow lost a bounded immutable original chunk")
    return value.payload


def _checkpoint_at(conn: Connection, source: ProviderTerminalHoldSource, checkpoint_id: str) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == source.session_id,
            checkpoints.c.operation_id == source.operation_id,
            checkpoints.c.checkpoint_id == checkpoint_id,
        )
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("checkpoint paid overflow lost its original checkpoint")
    payload = _bounded_bytes(conn, source, row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    return StoredContinuationSnapshot(payload, row.cursor, row.checkpoint_id)


def _event_at(conn: Connection, source: ProviderTerminalHoldSource, cursor: int) -> tuple[str, str]:
    row = conn.execute(
        select(events).where(
            events.c.session_id == source.session_id,
            events.c.operation_id == source.operation_id,
            events.c.sequence == cursor,
        )
    ).one_or_none()
    if row is None or row.payload_chunk_key != f"event:{cursor}":
        raise AuditIntegrityError("checkpoint paid overflow lost its exact journal event")
    return row.tag, _bounded_bytes(conn, source, row.payload_chunk_key, "event.v2").decode("utf-8", errors="strict")


def read_v4_checkpoint_paid_overflow_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str, names: frozenset[str] | None = None
) -> VerifiedCheckpointPaidOverflowTerminal | None:
    """Verify source, child, outer unknowns, journal, quota and H together."""
    if type(session_id) is not UUID or type(operation_id) is not str or type(effect_key) is not str:
        raise TypeError("checkpoint cold terminal requires exact original coordinates")
    if names is not None and (type(names) is not frozenset or any(type(name) is not str for name in names)):
        raise TypeError("checkpoint cold terminal requires actual current guard names")
    sid = str(session_id)
    source = _stored_hold_source(conn, sid, operation_id, effect_key)
    if source is None:
        return None
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one()
    if effect.disposition != "paid_overflow":
        return None
    if (
        effect.owner != "provider"
        or not effect_key.startswith("provider:1:")
        or effect.input_sha256 != source.source_input_sha256
        or effect.provider_attempt_id != hold.provider_attempt_id
        or type(effect.provider_attempt_id) is not str
        or type(effect.requested_model) is not str
        or type(effect.receipt_key) is not str
        or type(effect.result_chunk_key) is not str
        or (effect.creator_sequence, effect.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (effect.physical_sequence, effect.physical_generation) != (source.creator_sequence, source.creator_generation)
    ):
        raise AuditIntegrityError("checkpoint paid overflow changed its original child and H binding")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    if (
        physical_row is None
        or physical_row.session_id != sid
        or physical_row.source != "composer"
        or physical_row.run_id is not None
        or physical_row.operation_id != source.session_operation_id
        or physical_row.operation_epoch != source.session_operation_epoch
        or sha256(physical_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or physical_row.settled_at is None
        or physical_row.ledger_entry_id is None
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its original settled physical attempt")
    prepared = _checkpoint_at(conn, source, source.source_checkpoint_id)
    if (
        prepared.cursor != source.source_cursor
        or sha256(prepared.payload_json.encode("utf-8")).hexdigest() != source.source_snapshot_sha256
    ):
        raise AuditIntegrityError("checkpoint paid overflow changed its prepared source snapshot")
    preparation_tag, preparation_text = _event_at(conn, source, prepared.cursor)
    preparation, error = parse_json_strict(preparation_text)
    if (
        preparation_tag != "checkpoint"
        or error is not None
        or type(preparation) is not dict
        or set(preparation)
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
        or preparation["owner"] != "composer_checkpoint_source"
        or preparation["version"] != 1
        or preparation["stage"] != "source_prepared"
        or preparation["phase"] not in ("early", "end")
        or preparation["kind"] not in ("iteration_early", "iteration_end", "end_review")
        or (preparation["kind"] == "iteration_early") != (preparation["phase"] == "early")
        or type(preparation["pass_index"]) is not int
        or preparation["pass_index"] < 0
        or preparation["creator_sequence"] != source.creator_sequence
        or preparation["creator_generation"] != source.creator_generation
        or preparation["captured_root_sha256"] != sha256(prepared.payload_json.encode("utf-8")).hexdigest()
        or (preparation["phase"] == "end") != (preparation["end_input_chunk_key"] is not None)
        or (preparation["phase"] == "end") != (preparation["end_input_sha256"] is not None)
        or type(preparation["end_env_ref_names"]) is not list
        or any(type(name) is not str or not name for name in preparation["end_env_ref_names"])
        or preparation["end_env_ref_names"] != sorted(set(preparation["end_env_ref_names"]))
        or (preparation["phase"] == "early" and preparation["end_env_ref_names"] != [])
        or preparation_text != canonical_json(preparation)
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its original phase preparation event")
    origin = _checkpoint_at(conn, source, preparation["source_checkpoint_id"])
    prepared_root = decode_continuation_snapshot(prepared.payload_json)
    original_root = decode_continuation_snapshot(origin.payload_json)
    if (
        origin.cursor != preparation["source_cursor"]
        or prepared.cursor != origin.cursor + 1
        or prepared_root.model_copy(update={"frames": original_root.frames, "cursor": original_root.cursor}) != original_root
        or preparation["outer_effect_key"] not in {item.effect_id for item in original_root.effect_frontier}
        or not any(
            item.effect_id == preparation["outer_effect_key"] and item.disposition == "dispatched" for item in original_root.effect_frontier
        )
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its original unpaid outer phase")
    end_retry_ordinal: int | None = None
    if preparation["phase"] == "end":
        if names != frozenset(preparation["end_env_ref_names"]):
            raise AuditIntegrityError("checkpoint paid overflow changed current END guard inventory")
        end_key = preparation["end_input_chunk_key"]
        end_digest = preparation["end_input_sha256"]
        if type(end_key) is not str or type(end_digest) is not str or end_key != f"value:composer.checkpoint-end-input.v1:{end_digest}":
            raise AuditIntegrityError("checkpoint paid overflow changed its original END input reference")
        end_bytes = _bounded_bytes(conn, source, end_key, "composer.checkpoint-end-input.v1")
        if len(end_bytes) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES or sha256(end_bytes).hexdigest() != end_digest:
            raise AuditIntegrityError("checkpoint paid overflow changed its bounded END input bytes")
        end_data = decode_checkpoint_end_input(
            PrivateChunk(
                PrivateChunkRef(codec="composer.checkpoint-end-input.v1", digest=end_digest, byte_length=len(end_bytes)),
                end_bytes.decode("utf-8", errors="strict"),
            ),
            env_ref_names=frozenset(preparation["end_env_ref_names"]),
        )
        if (
            end_data.session_id != sid
            or end_data.operation_id != operation_id
            or end_data.original_checkpoint_id != origin.checkpoint_id
            or end_data.original_cursor != origin.cursor
            or end_data.outer_effect_key != preparation["outer_effect_key"]
            or end_data.pass_index != preparation["pass_index"]
            or end_data.captured_runtime_sha256
            != stable_hash(prepared_root.model_copy(update={"cursor": origin.cursor}).model_dump(mode="json"))
            or end_data.runtime_chunks
            != tuple(chunk.reference for chunk in load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=prepared_root))
        ):
            raise AuditIntegrityError("checkpoint paid overflow lost its complete original END gate input")
        end_retry_ordinal = end_data.retry_ordinal
    admitted_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor == prepared.cursor + 1,
        )
    ).one_or_none()
    if admitted_row is None or admitted_row.kind != "transition":
        raise AuditIntegrityError("checkpoint paid overflow lost its original paid dispatch")
    admitted = _checkpoint_at(conn, source, admitted_row.checkpoint_id)
    dispatch_tag, dispatch_text = _event_at(conn, source, admitted.cursor)
    dispatch, error = parse_json_strict(dispatch_text)
    if (
        dispatch_tag != "checkpoint"
        or error is not None
        or type(dispatch) is not dict
        or set(dispatch)
        != {
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
        or dispatch["owner"] != "composer_checkpoint_paid"
        or dispatch["version"] != 1
        or dispatch["stage"] != "dispatched"
        or dispatch["phase"] != preparation["phase"]
        or dispatch["kind"] != preparation["kind"]
        or dispatch["pass_index"] != preparation["pass_index"]
        or type(dispatch["retry_ordinal"]) is not int
        or dispatch["retry_ordinal"] < 0
        or dispatch["effect_key"] != effect_key
        or dispatch["outer_effect_key"] != preparation["outer_effect_key"]
        or dispatch["attempt_id"] != effect.provider_attempt_id
        or dispatch["request_sha256"] != source.source_input_sha256
        or dispatch["requested_model"] != effect.requested_model
        or dispatch["source_checkpoint_id"] != prepared.checkpoint_id
        or dispatch["source_cursor"] != prepared.cursor
        or dispatch["end_input_chunk_key"] != preparation["end_input_chunk_key"]
        or dispatch["end_input_sha256"] != preparation["end_input_sha256"]
        or dispatch["end_env_ref_names"] != preparation["end_env_ref_names"]
        or (preparation["phase"] == "end" and dispatch["retry_ordinal"] != end_retry_ordinal)
        or dispatch_text != canonical_json(dispatch)
    ):
        raise AuditIntegrityError("checkpoint paid overflow changed its original dispatch event")
    expected_dispatched = decode_continuation_snapshot(
        _effect_snapshot(
            prepared.payload_json,
            effect_key=effect_key,
            disposition="dispatched",
            receipt_id=None,
            add_attempt_id=effect.provider_attempt_id,
        )
    ).model_copy(update={"cursor": admitted.cursor})
    if admitted.cursor != prepared.cursor + 1 or decode_continuation_snapshot(admitted.payload_json) != expected_dispatched:
        raise AuditIntegrityError("checkpoint paid overflow changed its complete original admitted root")
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
    receipt_text = _bounded_bytes(conn, source, effect.result_chunk_key, "composer.paid-overflow.v1").decode("utf-8", errors="strict")
    receipt = decode_paid_overflow_receipt(receipt_text, expected=identity)
    if receipt.producer != "checkpoint_advisor":
        raise AuditIntegrityError("checkpoint paid overflow borrowed another producer receipt")
    combined = prepare_checkpoint_terminal_hold_plan(
        identity=identity,
        admitted=admitted,
        phase=preparation["phase"],
        creator_sequence=source.creator_sequence,
        requested_model=effect.requested_model,
    )
    if source.reserved_bytes != combined.reserved_bytes or source.fallback_contract_sha256 != combined.contract_sha256:
        raise AuditIntegrityError("checkpoint paid overflow changed its original pre-SDK H bound")
    plan = combined.paid_overflow
    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    terminal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == terminal.checkpoint_id,
        )
    ).one()
    batch = build_checkpoint_paid_overflow_tail_batch(plan, receipt=receipt, published_at=restore_utc(terminal_row.created_at))
    if (
        terminal_row.kind != "transition"
        or effect.receipt_key != batch.receipt_key
        or effect.result_chunk_key != batch.receipt_chunk_key
        or terminal.cursor != admitted.cursor + len(batch.append.events)
        or terminal.payload_json != batch.append.snapshot_json
        or terminal_row.snapshot_chunk_key != f"snapshot:{terminal.checkpoint_id}"
        or _bounded_bytes(conn, source, terminal_row.snapshot_chunk_key, "snapshot.v2") != terminal.payload_json.encode("utf-8")
        or parent.status != "failed"
        or parent.settled_by != "owner_terminal"
        or parent.failure_code != "http_error"
        or parent.checkpoint_id != terminal.checkpoint_id
        or parent.journal_cursor != terminal.cursor
        or parent.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR
        or parent.result_json != canonical_json(checkpoint_paid_overflow_error(parent.request_id, receipt.cost).model_dump(mode="json"))
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its exact failed terminal result")
    successors = conn.execute(
        select(checkpoints.c.checkpoint_id).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor > admitted.cursor,
        )
    ).all()
    if tuple(row.checkpoint_id for row in successors) != (terminal.checkpoint_id,):
        raise AuditIntegrityError("checkpoint paid overflow has another post-dispatch head")
    event_rows = conn.execute(
        select(events)
        .where(
            events.c.session_id == sid,
            events.c.operation_id == operation_id,
            events.c.sequence > admitted.cursor,
        )
        .order_by(events.c.sequence)
    ).all()
    if len(event_rows) != len(batch.append.events):
        raise AuditIntegrityError("checkpoint paid overflow changed terminal attribution length")
    for row, expected in zip(event_rows, batch.append.events, strict=True):
        if (
            row.tag != expected.tag
            or row.payload_chunk_key != f"event:{row.sequence}"
            or _bounded_bytes(conn, source, row.payload_chunk_key, "event.v2") != expected.payload_json.encode("utf-8")
        ):
            raise AuditIntegrityError("checkpoint paid overflow changed its exact terminal attribution")
    actual = set(
        conn.execute(
            select(effects.c.effect_key, effects.c.disposition, effects.c.receipt_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation_id
            )
        ).tuples()
    )
    root = decode_continuation_snapshot(terminal.payload_json)
    if actual != {(item.effect_id, item.disposition, item.receipt_id) for item in root.effect_frontier} or root.pending_settlement_ids:
        raise AuditIntegrityError("checkpoint paid overflow changed its complete terminal effect inventory")
    from elspeth.web.coordination.quota_authority import _usage_entry_id

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
        raise AuditIntegrityError("checkpoint paid overflow lost its original quota ledger")
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
    if (
        segment.state != "settled"
        or segment.generation != source.creator_generation
        or history.outcome != "settled"
        or history.revoked_at is None
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its closed original segment")
    return VerifiedCheckpointPaidOverflowTerminal(
        source, receipt, terminal, identity.attempt_id, hold.state, preparation["phase"], preparation["outer_effect_key"]
    )

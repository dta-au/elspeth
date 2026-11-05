"""Cold proof of one V4 EARLY/END returned paid/incomplete checkpoint."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_RESULT_SCHEMA_ERROR,
    ComposerOperationError,
    composer_operation_result_hash,
)
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    VerifiedCheckpointEndOwed,
    read_v4_checkpoint_end_settled_owed_history_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_audit import require_checkpoint_paid_public_audit_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid_incomplete_tail import (
    CheckpointPaidIncompleteTailBatch,
    CheckpointPaidIncompleteTailPlan,
    build_checkpoint_paid_incomplete_tail_batch,
)
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import require_checkpoint_paid_private_audit_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid_tail import (
    CheckpointPaidOverflowTailBatch,
    CheckpointPaidOverflowTailPlan,
    build_checkpoint_paid_overflow_tail_batch,
)
from elspeth.web.sessions.continuation_checkpoint_sdk_reservation import prepare_checkpoint_sdk_terminal_hold_plan
from elspeth.web.sessions.continuation_checkpoint_source_replay import _bounded_bytes as _bounded_bytes
from elspeth.web.sessions.continuation_checkpoint_source_replay import _checkpoint_at as _checkpoint_at
from elspeth.web.sessions.continuation_checkpoint_source_replay import _event_at as _event_at
from elspeth.web.sessions.continuation_checkpoint_source_replay import read_checkpoint_provider_source_on_connection
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt, decode_paid_incomplete_receipt
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal, paid_incomplete_error
from elspeth.web.sessions.continuation_paid_overflow import (
    PaidOverflowCost,
    PaidOverflowReceipt,
    decode_paid_overflow_receipt,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
)
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


@dataclass(frozen=True, slots=True)
class CheckpointPaidIncompleteTerminalEvidence:
    source: ProviderTerminalHoldSource
    receipt: PaidIncompleteReceipt
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


def checkpoint_end_owed_overflow_error(request_id: str | None, cost: PaidOverflowCost) -> ComposerOperationError:
    """The original paid response is retained; its required END tail is not."""
    if type(cost) is not PaidOverflowCost:
        raise TypeError("END owed overflow public error needs its bounded original cost")
    return ComposerOperationError(
        http_status=507,
        failure_code="http_error",
        error_type="paid_result_overflow",
        body={
            "error_type": "paid_result_overflow",
            "detail": "The paid result was retained, but its required review consequence could not be retained. It was not used or retried.",
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


def _read_v4_checkpoint_terminal_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    effect_key: str,
    disposition: Literal["paid_overflow", "paid_incomplete"],
    names: frozenset[str] | None = None,
    post_h: bool = False,
    post_h_hold_state: Literal["active", "consumed"] = "consumed",
) -> VerifiedCheckpointPaidOverflowTerminal | CheckpointPaidIncompleteTerminalEvidence | None:
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
    if effect.disposition != disposition:
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
    replay = read_checkpoint_provider_source_on_connection(conn, source=source, effect=effect, names=names)
    admitted, identity = replay.admitted, replay.identity
    codec = "composer.paid-overflow.v1" if disposition == "paid_overflow" else "composer.paid-incomplete.v1"
    receipt_text = _bounded_bytes(conn, source, effect.result_chunk_key, codec).decode("utf-8", errors="strict")
    if disposition == "paid_overflow":
        receipt: PaidOverflowReceipt | PaidIncompleteReceipt = decode_paid_overflow_receipt(receipt_text, expected=identity)
    else:
        receipt = decode_paid_incomplete_receipt(receipt_text, expected=identity)
    if receipt.producer != "checkpoint_advisor":
        raise AuditIntegrityError("checkpoint paid overflow borrowed another producer receipt")
    combined = prepare_checkpoint_sdk_terminal_hold_plan(
        identity=identity,
        admitted=admitted,
        phase=replay.phase,
        creator_sequence=source.creator_sequence,
        requested_model=effect.requested_model,
        historical_contract_sha256=None if post_h else source.fallback_contract_sha256,
        early_input=replay.early_input,
    )
    effective_admitted = admitted
    private_audit_incomplete = disposition == "paid_incomplete"
    early_owed_source = None
    plan: CheckpointPaidOverflowTailPlan | CheckpointPaidIncompleteTailPlan
    if post_h:
        from elspeth.web.sessions.continuation_checkpoint_early_return import (
            VerifiedEarlyCompleteReturn,
            read_early_overflow_return_history_on_connection,
        )

        if disposition != "paid_overflow" or replay.phase not in ("early", "end") or type(names) is not frozenset:
            raise AuditIntegrityError("post-H checkpoint overflow requires its actual phase and inventory")
        owed_row = conn.execute(
            select(checkpoints.c.checkpoint_id).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation_id,
                checkpoints.c.cursor == admitted.cursor + 1,
            )
        ).one_or_none()
        if owed_row is None:
            raise AuditIntegrityError("post-H checkpoint overflow lost its complete returned head")
        owed: VerifiedEarlyCompleteReturn | VerifiedCheckpointEndOwed
        if replay.phase == "early":
            owed = read_early_overflow_return_history_on_connection(
                conn,
                session_id=sid,
                operation_id=operation_id,
                selected=_checkpoint_at(conn, source, owed_row.checkpoint_id),
                names=names,
                hold_state=post_h_hold_state,
            )
            private_audit_incomplete = owed.call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
            early_owed_source = owed
        else:
            owed = read_v4_checkpoint_end_settled_owed_history_on_connection(
                conn,
                session_id=sid,
                operation_id=operation_id,
                attempt_id=identity.attempt_id,
                owed_checkpoint_id=owed_row.checkpoint_id,
                names=names,
                child_disposition="paid_overflow",
                hold_state=post_h_hold_state,
            )
        overflow_receipt = receipt
        if (
            owed.original_selected != admitted
            or owed.new_hold != source
            or owed.terminal_tail.identity != identity
            or type(overflow_receipt) is not PaidOverflowReceipt
            or overflow_receipt.representation != "retained_tail"
            or overflow_receipt.reason not in ("private_document_bytes", "evidence_pair_bytes", "retained_payload_bytes")
        ):
            raise AuditIntegrityError("post-H checkpoint overflow changed its complete response or missing tail")
        effective_admitted = owed.selected
        plan = owed.terminal_tail
    else:
        if source.reserved_bytes != combined.reserved_bytes or source.fallback_contract_sha256 != combined.contract_sha256:
            raise AuditIntegrityError("checkpoint paid overflow changed its original pre-SDK H bound")
        plan = combined.paid_overflow if disposition == "paid_overflow" else combined.paid_incomplete
    terminal_event_tag, terminal_event_text = _event_at(conn, source, effective_admitted.cursor + 1)
    terminal_event, terminal_event_error = parse_json_strict(terminal_event_text)
    if (
        terminal_event_tag != "checkpoint"
        or terminal_event_error is not None
        or type(terminal_event) is not dict
        or type(terminal_event.get("private_audit_sha256")) is not str
    ):
        raise AuditIntegrityError("checkpoint paid terminal lost its private audit event commitment")
    private_audit_sha256 = terminal_event["private_audit_sha256"]
    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    terminal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == terminal.checkpoint_id,
        )
    ).one()
    batch: CheckpointPaidOverflowTailBatch | CheckpointPaidIncompleteTailBatch
    if type(plan) is CheckpointPaidOverflowTailPlan and type(receipt) is PaidOverflowReceipt:
        batch = build_checkpoint_paid_overflow_tail_batch(
            plan, receipt=receipt, published_at=restore_utc(terminal_row.created_at), private_audit_sha256=private_audit_sha256
        )
        if post_h and replay.phase == "early":
            from elspeth.web.sessions.continuation_checkpoint_early_owed_overflow import checkpoint_early_owed_overflow_error

            expected_error = checkpoint_early_owed_overflow_error(parent.request_id, receipt.cost)
        else:
            expected_error = (
                checkpoint_end_owed_overflow_error(parent.request_id, receipt.cost)
                if post_h
                else checkpoint_paid_overflow_error(parent.request_id, receipt.cost)
            )
    elif type(plan) is CheckpointPaidIncompleteTailPlan and type(receipt) is PaidIncompleteReceipt:
        batch = build_checkpoint_paid_incomplete_tail_batch(
            plan, receipt=receipt, published_at=restore_utc(terminal_row.created_at), private_audit_sha256=private_audit_sha256
        )
        expected_error = paid_incomplete_error(parent.request_id, receipt.cost)
    else:
        raise AuditIntegrityError("checkpoint terminal crossed returned result dispositions")
    if (
        terminal_row.kind != "transition"
        or effect.receipt_key != batch.receipt_key
        or effect.result_chunk_key != batch.receipt_chunk_key
        or terminal.cursor != effective_admitted.cursor + len(batch.append.events)
        or terminal.payload_json != batch.append.snapshot_json
        or terminal_row.snapshot_chunk_key != f"snapshot:{terminal.checkpoint_id}"
        or _bounded_bytes(conn, source, terminal_row.snapshot_chunk_key, "snapshot.v2") != terminal.payload_json.encode("utf-8")
        or parent.status != "failed"
        or parent.settled_by != "owner_terminal"
        or parent.failure_code != "http_error"
        or parent.checkpoint_id != terminal.checkpoint_id
        or parent.journal_cursor != terminal.cursor
        or parent.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR
        or parent.result_json != canonical_json(expected_error.model_dump(mode="json"))
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its exact failed terminal result")
    successors = conn.execute(
        select(checkpoints.c.checkpoint_id)
        .where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor > admitted.cursor,
        )
        .order_by(checkpoints.c.cursor)
    ).all()
    expected_successors = (effective_admitted.checkpoint_id, terminal.checkpoint_id) if post_h else (terminal.checkpoint_id,)
    if tuple(row.checkpoint_id for row in successors) != expected_successors:
        raise AuditIntegrityError("checkpoint paid overflow has another post-dispatch head")
    event_rows = conn.execute(
        select(events)
        .where(
            events.c.session_id == sid,
            events.c.operation_id == operation_id,
            events.c.sequence > effective_admitted.cursor,
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
    require_checkpoint_paid_public_audit_on_connection(
        conn,
        source=source,
        attempt_id=identity.attempt_id,
        requested_model=effect.requested_model,
        physical_started_at=physical_row.started_at,
        physical_settled_at=physical_row.settled_at,
        response_finished_at=effect.response_finished_at,
        receipt=receipt,
        early_owed=early_owed_source,
    )
    require_checkpoint_paid_private_audit_on_connection(
        conn,
        source=source,
        attempt_id=identity.attempt_id,
        private_sha256=private_audit_sha256,
        incomplete=private_audit_incomplete,
    )
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation_id, segments.c.sequence == source.creator_sequence
        )
    ).one()
    settled_generation = parent.ownership_generation if post_h and replay.phase == "end" else source.creator_generation
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == settled_generation,
        )
    ).one()
    if post_h and replay.phase == "end":
        from elspeth.web.sessions.continuation_checkpoint_end_recovery import require_end_h_anchored_generation_chain_on_connection

        if type(owed) is not VerifiedCheckpointEndOwed:
            raise AuditIntegrityError("END overflow changed its exact historical END source")
        require_end_h_anchored_generation_chain_on_connection(conn, owed=owed, generation=settled_generation)
        if (
            history.session_operation_id != parent.session_operation_id
            or history.session_operation_epoch != parent.session_operation_epoch
            or history.session_operation_lease_token != parent.session_operation_lease_token
            or history.claim_attempt != parent.attempt
        ):
            raise AuditIntegrityError("checkpoint END overflow lost its final replacement fence")
    elif post_h and (
        parent.attempt != source.claim_attempt
        or parent.session_operation_id != source.session_operation_id
        or parent.session_operation_epoch != source.session_operation_epoch
        or type(parent.session_operation_lease_token) is not str
        or sha256(parent.session_operation_lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
    ):
        raise AuditIntegrityError("EARLY owed overflow cannot borrow a replacement writer")
    if (
        segment.state != "settled"
        or segment.generation != settled_generation
        or history.outcome != "settled"
        or history.revoked_at is None
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != settled_generation
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its closed original segment")
    if type(receipt) is PaidOverflowReceipt:
        return VerifiedCheckpointPaidOverflowTerminal(
            source, receipt, terminal, identity.attempt_id, hold.state, replay.phase, replay.outer_effect_key
        )
    if type(receipt) is not PaidIncompleteReceipt:
        raise AuditIntegrityError("checkpoint incomplete terminal lost its distinct receipt")
    return CheckpointPaidIncompleteTerminalEvidence(
        source, receipt, terminal, identity.attempt_id, hold.state, replay.phase, replay.outer_effect_key
    )


def read_v4_checkpoint_paid_overflow_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str, names: frozenset[str] | None = None
) -> VerifiedCheckpointPaidOverflowTerminal | None:
    result = _read_v4_checkpoint_terminal_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        effect_key=effect_key,
        disposition="paid_overflow",
        names=names,
    )
    if result is None or type(result) is VerifiedCheckpointPaidOverflowTerminal:
        return result
    raise AuditIntegrityError("checkpoint overflow reader crossed its terminal disposition")


def read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    effect_key: str,
    names: frozenset[str],
    hold_state: Literal["active", "consumed"] = "consumed",
) -> VerifiedCheckpointPaidOverflowTerminal | None:
    result = _read_v4_checkpoint_terminal_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        effect_key=effect_key,
        disposition="paid_overflow",
        names=names,
        post_h=True,
        post_h_hold_state=hold_state,
    )
    if result is None or (type(result) is VerifiedCheckpointPaidOverflowTerminal and result.phase == "end"):
        return result
    raise AuditIntegrityError("END owed overflow reader crossed its terminal disposition")


def read_v4_checkpoint_paid_incomplete_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str, names: frozenset[str] | None = None
) -> CheckpointPaidIncompleteTerminalEvidence | None:
    result = _read_v4_checkpoint_terminal_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        effect_key=effect_key,
        disposition="paid_incomplete",
        names=names,
    )
    if result is None or type(result) is CheckpointPaidIncompleteTerminalEvidence:
        return result
    raise AuditIntegrityError("checkpoint incomplete reader crossed its terminal disposition")


def read_v4_checkpoint_paid_incomplete_verified_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str, names: frozenset[str] | None = None
) -> VerifiedPaidIncompleteTerminal | None:
    evidence = read_v4_checkpoint_paid_incomplete_terminal_on_connection(
        conn, session_id=session_id, operation_id=operation_id, effect_key=effect_key, names=names
    )
    if evidence is None:
        return None
    if evidence.hold_state != "consumed":
        raise AuditIntegrityError("checkpoint incomplete terminal has not consumed original H")
    return VerifiedPaidIncompleteTerminal(evidence.source, evidence.receipt, evidence.terminal, evidence.provider_attempt_id, "consumed")

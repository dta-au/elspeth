"""Transaction-local exact H credit for a checkpoint returned-overflow tail."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection
from sqlalchemy.engine import RootTransaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.sessions.continuation import PreparedContinuationAppend, StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_checkpoint_no_sdk_tail import (
    CheckpointNoSdkTailBatch,
    CheckpointNoSdkTailPlan,
    build_checkpoint_no_sdk_tail_batch,
)
from elspeth.web.sessions.continuation_checkpoint_paid_incomplete_tail import (
    CheckpointPaidIncompleteTailBatch,
    CheckpointPaidIncompleteTailPlan,
    build_checkpoint_paid_incomplete_tail_batch,
)
from elspeth.web.sessions.continuation_checkpoint_paid_tail import (
    CheckpointPaidOverflowTailBatch,
    CheckpointPaidOverflowTailPlan,
    build_checkpoint_paid_overflow_tail_batch,
)
from elspeth.web.sessions.continuation_checkpoint_sdk_reservation import prepare_checkpoint_sdk_terminal_hold_plan
from elspeth.web.sessions.continuation_checkpoint_terminal_hold import (
    CheckpointTerminalHoldPlan,
)
from elspeth.web.sessions.continuation_paid_incomplete import decode_paid_incomplete_receipt
from elspeth.web.sessions.continuation_paid_overflow import decode_paid_overflow_receipt
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    admit_retained_payload_chunks,
)

_SCOPE_KEY = "elspeth.v4_checkpoint_paid_overflow_tail"


@dataclass(slots=True)
class _CheckpointPaidTailScope:
    connection: Connection
    transaction: RootTransaction
    source: ProviderTerminalHoldSource
    plan: CheckpointPaidOverflowTailPlan | CheckpointPaidIncompleteTailPlan | CheckpointNoSdkTailPlan
    batch: CheckpointPaidOverflowTailBatch | CheckpointPaidIncompleteTailBatch | CheckpointNoSdkTailBatch
    stage: int = 0


def _current(conn: Connection, session_id: str, operation_id: str) -> _CheckpointPaidTailScope | None:
    scope = conn.info.get(_SCOPE_KEY)
    if scope is None:
        return None
    if (
        type(scope) is not _CheckpointPaidTailScope
        or scope.connection is not conn
        or scope.transaction is not conn.get_transaction()
        or not scope.transaction.is_active
        or (scope.source.session_id, scope.source.operation_id) != (session_id, operation_id)
    ):
        raise AuditIntegrityError("checkpoint paid tail crossed its original transaction or operation")
    return scope


def checkpoint_paid_tail_credit_for_chunk(
    conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk
) -> ProviderTerminalHoldSource | None:
    scope = _current(conn, session_id, operation_id)
    if scope is None:
        return None
    if scope.stage != 0 or candidate != scope.batch.prospective[0]:
        raise AuditIntegrityError("checkpoint paid tail chunk differs from its exact admitted receipt")
    return scope.source


def record_checkpoint_paid_tail_chunk(
    conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk
) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is not None:
        checkpoint_paid_tail_credit_for_chunk(conn, session_id=session_id, operation_id=operation_id, candidate=candidate)
        scope.stage = 1


def checkpoint_paid_tail_credit_for_append(
    conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None
) -> ProviderTerminalHoldSource | None:
    scope = _current(conn, session_id, operation_id)
    if scope is None:
        return None
    if scope.stage != 1 or append != scope.batch.append:
        raise AuditIntegrityError("checkpoint paid tail append differs from its exact admitted snapshot")
    scope.batch.append.require_original()
    return scope.source


def record_checkpoint_paid_tail_append(
    conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None
) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is not None:
        checkpoint_paid_tail_credit_for_append(conn, session_id=session_id, operation_id=operation_id, append=append)
        scope.stage = 2


def record_checkpoint_paid_tail_hold_consumed(
    conn: Connection, *, session_id: str, operation_id: str, source: ProviderTerminalHoldSource
) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is None or scope.stage != 2 or source != scope.source:
        raise AuditIntegrityError("checkpoint paid tail was not consumed by its exact batch")
    scope.stage = 3


@contextmanager
def admit_v4_checkpoint_paid_tail_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    hold_plan: CheckpointTerminalHoldPlan,
    batch: CheckpointPaidOverflowTailBatch | CheckpointPaidIncompleteTailBatch,
) -> Iterator[None]:
    """Replace H with exactly one receipt and one complete terminal append."""
    if type(source) is not ProviderTerminalHoldSource or type(hold_plan) is not CheckpointTerminalHoldPlan:
        raise AuditIntegrityError("checkpoint paid tail requires nominal original hold and plan")
    if type(batch) is CheckpointPaidOverflowTailBatch:
        plan: CheckpointPaidOverflowTailPlan | CheckpointPaidIncompleteTailPlan = hold_plan.paid_overflow
    elif type(batch) is CheckpointPaidIncompleteTailBatch:
        plan = hold_plan.paid_incomplete
    else:
        raise AuditIntegrityError("checkpoint paid tail requires its exact returned batch type")
    if _SCOPE_KEY in conn.info:
        raise AuditIntegrityError("checkpoint paid tail requires its sole exact batch")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("checkpoint paid tail requires its original root transaction")
    if (
        (source.session_id, source.operation_id) != (plan.identity.session_id, plan.identity.operation_id)
        or source.effect_key != plan.identity.effect_key
        or source.policy_sha256 != plan.identity.policy_sha256
        or source.session_operation_id != plan.identity.session_operation_id
        or source.session_operation_epoch != plan.identity.session_operation_epoch
        or source.creator_generation != plan.identity.ownership_generation
        or source.creator_sequence != plan.creator_sequence
        or source.lease_sha256 != plan.identity.lease_sha256
        or source.source_checkpoint_id != plan.identity.source_checkpoint_id
        or source.source_cursor != plan.identity.source_cursor
        or source.source_snapshot_sha256 != plan.identity.source_snapshot_sha256
        or source.reserved_bytes != hold_plan.reserved_bytes
        or source.fallback_contract_sha256 != hold_plan.contract_sha256
        or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != plan.admitted
        or prepare_checkpoint_sdk_terminal_hold_plan(
            identity=plan.identity,
            admitted=plan.admitted,
            phase=plan.phase,
            creator_sequence=plan.creator_sequence,
            requested_model=hold_plan.no_sdk.requested_model,
            historical_contract_sha256=source.fallback_contract_sha256,
        )
        != hold_plan
    ):
        raise AuditIntegrityError("checkpoint paid tail lost its original hold, policy or admitted head")
    batch.append.require_original()
    try:
        checkpoint_uuid = UUID(batch.append.checkpoint_id)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("checkpoint paid tail append lost its minted checkpoint identity") from error
    if checkpoint_uuid.version != 4 or str(checkpoint_uuid) != batch.append.checkpoint_id:
        raise AuditIntegrityError("checkpoint paid tail append lost its minted checkpoint identity")
    if not batch.append.events or batch.append.events[0].tag != "checkpoint":
        raise AuditIntegrityError("checkpoint paid tail lacks its exact private audit event")
    event, event_error = parse_json_strict(batch.append.events[0].payload_json)
    if event_error is not None or type(event) is not dict or type(event.get("private_audit_sha256")) is not str:
        raise AuditIntegrityError("checkpoint paid tail lacks its exact private audit digest")
    private_audit_sha256 = event["private_audit_sha256"]
    reference: CheckpointPaidOverflowTailBatch | CheckpointPaidIncompleteTailBatch
    if type(plan) is CheckpointPaidOverflowTailPlan and type(batch) is CheckpointPaidOverflowTailBatch:
        receipt = decode_paid_overflow_receipt(batch.receipt_json, expected=plan.identity)
        reference = build_checkpoint_paid_overflow_tail_batch(
            plan, receipt=receipt, published_at=batch.append.published_at, private_audit_sha256=private_audit_sha256
        )
    elif type(plan) is CheckpointPaidIncompleteTailPlan and type(batch) is CheckpointPaidIncompleteTailBatch:
        incomplete_receipt = decode_paid_incomplete_receipt(batch.receipt_json, expected=plan.identity)
        reference = build_checkpoint_paid_incomplete_tail_batch(
            plan, receipt=incomplete_receipt, published_at=batch.append.published_at, private_audit_sha256=private_audit_sha256
        )
    else:
        raise AuditIntegrityError("checkpoint paid tail crossed terminal alternatives")
    expected = reference.append
    if (batch.receipt_json, batch.receipt_key, batch.receipt_chunk_key) != (
        reference.receipt_json,
        reference.receipt_key,
        reference.receipt_chunk_key,
    ) or (
        batch.append.session_id,
        batch.append.operation_id,
        batch.append.expected_checkpoint_id,
        batch.append.expected_cursor,
        batch.append.snapshot_json,
        batch.append.events,
        batch.append.published_at,
        batch.append.handoff,
        batch.append.predecessor_json,
    ) != (
        expected.session_id,
        expected.operation_id,
        expected.expected_checkpoint_id,
        expected.expected_cursor,
        expected.snapshot_json,
        expected.events,
        expected.published_at,
        expected.handoff,
        expected.predecessor_json,
    ):
        raise AuditIntegrityError("checkpoint paid tail changed its exact batch bytes")
    prospective = (
        reference.prospective[0],
        *reference.prospective[1:-1],
        ProspectiveContinuationChunk(f"snapshot:{batch.append.checkpoint_id}", batch.append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if batch.prospective != prospective or sum(len(chunk.payload) for chunk in prospective) > source.reserved_bytes:
        raise AuditIntegrityError("checkpoint paid tail exceeded its original bound")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )
    scope = _CheckpointPaidTailScope(conn, transaction, source, plan, batch)
    conn.info[_SCOPE_KEY] = scope
    try:
        yield
        final = StoredContinuationSnapshot(
            batch.append.snapshot_json, batch.append.expected_cursor + len(batch.append.events), batch.append.checkpoint_id
        )
        if scope.stage != 3 or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != final:
            raise AuditIntegrityError("checkpoint paid tail did not consume its complete batch and hold")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("checkpoint paid tail lost its transaction scope")


@contextmanager
def admit_v4_checkpoint_end_owed_overflow_tail_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    plan: CheckpointPaidOverflowTailPlan,
    batch: CheckpointPaidOverflowTailBatch,
) -> Iterator[None]:
    """Spend only the rebound H on one exact complete-response consequence overflow.

    The original provider response remains in the preceding END-owed checkpoint.
    This scope reserves the missing retained tail and enforces the same
    chunk/append/hold-consumption sequence as the pre-H terminal scope.
    """
    if (
        type(source) is not ProviderTerminalHoldSource
        or type(plan) is not CheckpointPaidOverflowTailPlan
        or type(batch) is not CheckpointPaidOverflowTailBatch
        or _SCOPE_KEY in conn.info
    ):
        raise AuditIntegrityError("END-owed overflow requires one nominal rebound H batch")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("END-owed overflow requires its original root transaction")
    if (
        plan.phase != "end"
        or source.session_id != plan.identity.session_id
        or source.operation_id != plan.identity.operation_id
        or source.effect_key != plan.identity.effect_key
        or source.policy_sha256 != plan.identity.policy_sha256
        or source.session_operation_id != plan.identity.session_operation_id
        or source.session_operation_epoch != plan.identity.session_operation_epoch
        or source.creator_generation != plan.identity.ownership_generation
        or source.creator_sequence != plan.creator_sequence
        or source.lease_sha256 != plan.identity.lease_sha256
        or source.source_checkpoint_id != plan.identity.source_checkpoint_id
        or source.source_cursor != plan.identity.source_cursor
        or source.source_snapshot_sha256 != plan.identity.source_snapshot_sha256
        or source.reserved_bytes != plan.reserved_bytes
        or source.fallback_contract_sha256 != plan.fallback_contract_sha256
        or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != plan.admitted
    ):
        raise AuditIntegrityError("END-owed overflow changed its exact rebound H contract")
    batch.append.require_original()
    event, error = parse_json_strict(batch.append.events[0].payload_json) if batch.append.events else (None, "missing")
    if error is not None or type(event) is not dict or type(event.get("private_audit_sha256")) is not str:
        raise AuditIntegrityError("END-owed overflow lost its original private audit digest")
    receipt = decode_paid_overflow_receipt(batch.receipt_json, expected=plan.identity)
    reference = build_checkpoint_paid_overflow_tail_batch(
        plan, receipt=receipt, published_at=batch.append.published_at, private_audit_sha256=event["private_audit_sha256"]
    )
    expected = reference.append
    if (
        (batch.receipt_json, batch.receipt_key, batch.receipt_chunk_key)
        != (reference.receipt_json, reference.receipt_key, reference.receipt_chunk_key)
        or (
            batch.append.session_id,
            batch.append.operation_id,
            batch.append.expected_checkpoint_id,
            batch.append.expected_cursor,
            batch.append.snapshot_json,
            batch.append.events,
            batch.append.published_at,
            batch.append.handoff,
            batch.append.predecessor_json,
        )
        != (
            expected.session_id,
            expected.operation_id,
            expected.expected_checkpoint_id,
            expected.expected_cursor,
            expected.snapshot_json,
            expected.events,
            expected.published_at,
            expected.handoff,
            expected.predecessor_json,
        )
        or batch.prospective
        != (
            *reference.prospective[:-1],
            ProspectiveContinuationChunk(
                f"snapshot:{batch.append.checkpoint_id}", batch.append.snapshot_json.encode("utf-8"), "snapshot.v2"
            ),
        )
        or sum(len(chunk.payload) for chunk in batch.prospective) > source.reserved_bytes
    ):
        raise AuditIntegrityError("END-owed overflow changed its exact bounded receipt tail")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )
    scope = _CheckpointPaidTailScope(conn, transaction, source, plan, batch)
    conn.info[_SCOPE_KEY] = scope
    try:
        yield
        final = StoredContinuationSnapshot(
            batch.append.snapshot_json, batch.append.expected_cursor + len(batch.append.events), batch.append.checkpoint_id
        )
        if scope.stage != 3 or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != final:
            raise AuditIntegrityError("END-owed overflow did not consume its complete batch and H")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("END-owed overflow lost its transaction scope")


@contextmanager
def admit_v4_checkpoint_no_sdk_tail_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    hold_plan: CheckpointTerminalHoldPlan,
    batch: CheckpointNoSdkTailBatch,
) -> Iterator[None]:
    """Spend the original combined H on exactly one issued zero-use terminal."""
    from elspeth.contracts.json_parser import parse_json_strict
    from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkRefusalReason

    if (
        type(source) is not ProviderTerminalHoldSource
        or type(hold_plan) is not CheckpointTerminalHoldPlan
        or type(batch) is not CheckpointNoSdkTailBatch
        or _SCOPE_KEY in conn.info
    ):
        raise AuditIntegrityError("checkpoint no-SDK tail requires one nominal original batch")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("checkpoint no-SDK tail requires its original root transaction")
    plan = hold_plan.no_sdk
    if plan.phase not in ("early", "end"):
        raise AuditIntegrityError("checkpoint no-SDK tail has no closed phase")
    phase: Literal["early", "end"] = "early" if plan.phase == "early" else "end"
    if (
        source.session_id != plan.identity.session_id
        or source.operation_id != plan.identity.operation_id
        or source.effect_key != plan.identity.effect_key
        or source.source_checkpoint_id != plan.identity.source_checkpoint_id
        or source.source_cursor != plan.identity.source_cursor
        or source.source_snapshot_sha256 != plan.identity.source_snapshot_sha256
        or source.reserved_bytes != hold_plan.reserved_bytes
        or source.fallback_contract_sha256 != hold_plan.contract_sha256
        or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != plan.admitted
        or prepare_checkpoint_sdk_terminal_hold_plan(
            identity=plan.identity,
            admitted=plan.admitted,
            phase=phase,
            creator_sequence=plan.creator_sequence,
            requested_model=plan.requested_model,
            historical_contract_sha256=source.fallback_contract_sha256,
        )
        != hold_plan
    ):
        raise AuditIntegrityError("checkpoint no-SDK tail lost its original H/source")
    data, error = parse_json_strict(batch.receipt_json)
    if error is not None or type(data) is not dict or data.get("reason") not in {reason.value for reason in CheckpointNoSdkRefusalReason}:
        raise AuditIntegrityError("checkpoint no-SDK tail lost its closed refusal reason")
    reason = CheckpointNoSdkRefusalReason(data["reason"])
    reference = build_checkpoint_no_sdk_tail_batch(
        plan, reason=reason, receipt_key=batch.receipt_key, published_at=batch.append.published_at
    )
    expected = reference.append
    batch.append.require_original()
    try:
        checkpoint_uuid = UUID(batch.append.checkpoint_id)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("checkpoint no-SDK tail lost its minted checkpoint identity") from error
    if checkpoint_uuid.version != 4 or str(checkpoint_uuid) != batch.append.checkpoint_id:
        raise AuditIntegrityError("checkpoint no-SDK tail lost its minted checkpoint identity")
    if (
        (batch.receipt_json, batch.receipt_key, batch.receipt_chunk_key)
        != (reference.receipt_json, reference.receipt_key, reference.receipt_chunk_key)
        or (
            batch.append.session_id,
            batch.append.operation_id,
            batch.append.expected_checkpoint_id,
            batch.append.expected_cursor,
            batch.append.snapshot_json,
            batch.append.events,
            batch.append.published_at,
            batch.append.handoff,
            batch.append.predecessor_json,
        )
        != (
            expected.session_id,
            expected.operation_id,
            expected.expected_checkpoint_id,
            expected.expected_cursor,
            expected.snapshot_json,
            expected.events,
            expected.published_at,
            expected.handoff,
            expected.predecessor_json,
        )
        or batch.prospective
        != (
            reference.prospective[0],
            *reference.prospective[1:-1],
            ProspectiveContinuationChunk(
                f"snapshot:{batch.append.checkpoint_id}", batch.append.snapshot_json.encode("utf-8"), "snapshot.v2"
            ),
        )
    ):
        raise AuditIntegrityError("checkpoint no-SDK tail changed its exact admitted bytes")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )
    scope = _CheckpointPaidTailScope(conn, transaction, source, plan, batch)
    conn.info[_SCOPE_KEY] = scope
    try:
        yield
        final = StoredContinuationSnapshot(
            batch.append.snapshot_json, batch.append.expected_cursor + len(batch.append.events), batch.append.checkpoint_id
        )
        if scope.stage != 3 or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != final:
            raise AuditIntegrityError("checkpoint no-SDK tail did not commit its complete batch and H")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("checkpoint no-SDK tail lost its transaction scope")

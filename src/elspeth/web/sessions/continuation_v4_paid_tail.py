"""Exact transaction-local V4 TITLE paid-overflow terminal batch custody.

The specialized TITLE owner proves the original fence, paid observation and
terminal SQL state. This scope only replaces one active hold with the exact
four immutable chunks it reserved. It grants no generic capacity bypass.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection
from sqlalchemy.engine import RootTransaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkRefusalReason
from elspeth.web.sessions.continuation import PreparedContinuationAppend, StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_paid_incomplete import decode_paid_incomplete_receipt
from elspeth.web.sessions.continuation_paid_overflow import decode_paid_overflow_receipt
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    admit_retained_payload_chunks,
)
from elspeth.web.sessions.continuation_title_no_sdk_tail import (
    TitleNoSdkTailBatch,
    TitleNoSdkTailPlan,
    build_title_no_sdk_tail_batch,
    prepare_title_no_sdk_tail_plan,
)
from elspeth.web.sessions.continuation_title_paid_incomplete_tail import (
    TitlePaidIncompleteTailBatch,
    TitlePaidIncompleteTailPlan,
    build_title_paid_incomplete_tail_batch,
    prepare_title_paid_incomplete_tail_plan,
    title_combined_terminal_hold_contract,
)
from elspeth.web.sessions.continuation_title_paid_overflow_tail import (
    TitlePaidOverflowTailBatch,
    TitlePaidOverflowTailPlan,
    build_title_paid_overflow_tail_batch,
    prepare_title_paid_overflow_tail_plan,
)

_SCOPE_KEY = "elspeth.v4_title_terminal_tail"


@dataclass(slots=True)
class _PaidTailScope:
    connection: Connection
    transaction: RootTransaction
    source: ProviderTerminalHoldSource
    plan: TitlePaidOverflowTailPlan | TitlePaidIncompleteTailPlan | TitleNoSdkTailPlan
    batch: TitlePaidOverflowTailBatch | TitlePaidIncompleteTailBatch | TitleNoSdkTailBatch
    stage: int = 0


def _current(conn: Connection, session_id: str, operation_id: str) -> _PaidTailScope | None:
    scope = conn.info.get(_SCOPE_KEY)
    if scope is None:
        return None
    if (
        type(scope) is not _PaidTailScope
        or scope.connection is not conn
        or scope.transaction is not conn.get_transaction()
        or not scope.transaction.is_active
        or (scope.source.session_id, scope.source.operation_id) != (session_id, operation_id)
    ):
        raise AuditIntegrityError("V4 paid terminal tail crossed its original transaction or operation")
    return scope


def paid_tail_credit_for_chunk(
    conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk
) -> ProviderTerminalHoldSource | None:
    scope = _current(conn, session_id, operation_id)
    if scope is None:
        return None
    if scope.stage != 0 or candidate != scope.batch.prospective[0]:
        raise AuditIntegrityError("V4 paid terminal chunk differs from its exact admitted receipt")
    return scope.source


def record_paid_tail_chunk(conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is not None:
        paid_tail_credit_for_chunk(conn, session_id=session_id, operation_id=operation_id, candidate=candidate)
        scope.stage = 1


def paid_tail_credit_for_append(
    conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None
) -> ProviderTerminalHoldSource | None:
    scope = _current(conn, session_id, operation_id)
    if scope is None:
        return None
    if scope.stage != 1 or append != scope.batch.append:
        raise AuditIntegrityError("V4 paid terminal append differs from its exact admitted snapshot")
    scope.batch.append.require_original()
    return scope.source


def record_paid_tail_append(conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is not None:
        paid_tail_credit_for_append(conn, session_id=session_id, operation_id=operation_id, append=append)
        scope.stage = 2


def record_paid_tail_hold_consumed(conn: Connection, *, session_id: str, operation_id: str, source: ProviderTerminalHoldSource) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is None or scope.stage != 2 or source != scope.source:
        raise AuditIntegrityError("V4 paid terminal hold was not consumed by its exact batch")
    scope.stage = 3


@contextmanager
def admit_v4_title_paid_tail_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    plan: TitlePaidOverflowTailPlan,
    incomplete_plan: TitlePaidIncompleteTailPlan,
    no_sdk_plan: TitleNoSdkTailPlan,
    batch: TitlePaidOverflowTailBatch,
) -> Iterator[None]:
    """Replace H with exactly one receipt and one two-event/full-root append.

    The SQL caller must already hold the original session lock and prove the
    current owner/fence. Every prefix writer rechecks capacity with this same
    bound hold. A caught or uncaught partial write rolls back the root txn.
    """
    if (
        type(source) is not ProviderTerminalHoldSource
        or type(plan) is not TitlePaidOverflowTailPlan
        or type(incomplete_plan) is not TitlePaidIncompleteTailPlan
        or type(no_sdk_plan) is not TitleNoSdkTailPlan
    ):
        raise TypeError("V4 paid terminal tail requires owned hold and plan")
    if type(batch) is not TitlePaidOverflowTailBatch or _SCOPE_KEY in conn.info:
        raise AuditIntegrityError("V4 paid terminal tail requires its sole exact batch")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("V4 paid terminal tail requires its original root transaction")
    root = decode_continuation_snapshot(plan.admitted.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    reserved_bytes, contract_sha256 = title_combined_terminal_hold_contract(plan, incomplete_plan, no_sdk_plan)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or (source.session_id, source.operation_id) != (plan.identity.session_id, plan.identity.operation_id)
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
        or source.reserved_bytes != reserved_bytes
        or source.fallback_contract_sha256 != contract_sha256
        or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != plan.admitted
    ):
        raise AuditIntegrityError("V4 paid terminal tail lost its original hold, policy or admitted head")
    if (
        prepare_title_paid_overflow_tail_plan(identity=plan.identity, admitted=plan.admitted, creator_sequence=plan.creator_sequence)
        != plan
    ):
        raise AuditIntegrityError("V4 paid terminal plan changed its original source-specific bound")
    batch.append.require_original()
    try:
        checkpoint_uuid = UUID(batch.append.checkpoint_id)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("V4 paid terminal append lost its minted checkpoint identity") from error
    if checkpoint_uuid.version != 4 or str(checkpoint_uuid) != batch.append.checkpoint_id:
        raise AuditIntegrityError("V4 paid terminal append lost its minted checkpoint identity")
    receipt = decode_paid_overflow_receipt(batch.receipt_json, expected=plan.identity)
    reference = build_title_paid_overflow_tail_batch(plan, receipt=receipt, published_at=batch.append.published_at)
    append = batch.append
    expected_append = reference.append
    if (batch.receipt_json, batch.receipt_key, batch.receipt_chunk_key) != (
        reference.receipt_json,
        reference.receipt_key,
        reference.receipt_chunk_key,
    ) or (
        append.session_id,
        append.operation_id,
        append.expected_checkpoint_id,
        append.expected_cursor,
        append.snapshot_json,
        append.events,
        append.published_at,
        append.handoff,
        append.predecessor_json,
    ) != (
        expected_append.session_id,
        expected_append.operation_id,
        expected_append.expected_checkpoint_id,
        expected_append.expected_cursor,
        expected_append.snapshot_json,
        expected_append.events,
        expected_append.published_at,
        expected_append.handoff,
        expected_append.predecessor_json,
    ):
        raise AuditIntegrityError("V4 paid terminal batch changed its complete original bytes")
    expected_chunks = (
        reference.prospective[0],
        *reference.prospective[1:-1],
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if batch.prospective != expected_chunks:
        raise AuditIntegrityError("V4 paid terminal batch changed an immutable chunk key or byte")
    if sum(len(chunk.payload) for chunk in batch.prospective) > source.reserved_bytes:
        raise AuditIntegrityError("V4 paid terminal batch exceeded its original hold")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )
    scope = _PaidTailScope(conn, transaction, source, plan, batch)
    conn.info[_SCOPE_KEY] = scope
    try:
        yield
        expected = StoredContinuationSnapshot(
            batch.append.snapshot_json, batch.append.expected_cursor + len(batch.append.events), batch.append.checkpoint_id
        )
        if scope.stage != 3 or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != expected:
            raise AuditIntegrityError("V4 paid terminal tail did not consume its complete batch and hold")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("V4 paid terminal tail lost its original transaction scope")


@contextmanager
def admit_v4_title_no_sdk_tail_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    paid_plan: TitlePaidOverflowTailPlan,
    incomplete_plan: TitlePaidIncompleteTailPlan,
    plan: TitleNoSdkTailPlan,
    batch: TitleNoSdkTailBatch,
) -> Iterator[None]:
    """Credit only the original bounded zero-use TITLE refusal batch."""
    if (
        type(source) is not ProviderTerminalHoldSource
        or type(paid_plan) is not TitlePaidOverflowTailPlan
        or type(incomplete_plan) is not TitlePaidIncompleteTailPlan
        or type(plan) is not TitleNoSdkTailPlan
        or type(batch) is not TitleNoSdkTailBatch
        or _SCOPE_KEY in conn.info
    ):
        raise AuditIntegrityError("V4 TITLE no-SDK tail requires one original closed scope")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("V4 TITLE no-SDK tail requires the original root transaction")
    root = decode_continuation_snapshot(plan.admitted.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    reserved_bytes, contract_sha256 = title_combined_terminal_hold_contract(paid_plan, incomplete_plan, plan)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or (source.session_id, source.operation_id) != (plan.identity.session_id, plan.identity.operation_id)
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
        or source.reserved_bytes != reserved_bytes
        or source.fallback_contract_sha256 != contract_sha256
        or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != plan.admitted
        or prepare_title_no_sdk_tail_plan(
            identity=plan.identity,
            admitted=plan.admitted,
            creator_sequence=plan.creator_sequence,
            requested_model=plan.requested_model,
        )
        != plan
    ):
        raise AuditIntegrityError("V4 TITLE no-SDK tail lost its original hold, policy or admitted head")
    batch.append.require_original()
    receipt, error = parse_json_strict(batch.receipt_json)
    if error is not None or type(receipt) is not dict or type(receipt.get("reason")) is not str:
        raise AuditIntegrityError("V4 TITLE no-SDK tail lacks its closed original receipt")
    try:
        reason = TitleNoSdkRefusalReason(receipt["reason"])
    except ValueError as exc:
        raise AuditIntegrityError("V4 TITLE no-SDK tail has an unknown refusal reason") from exc
    reference = build_title_no_sdk_tail_batch(plan, reason=reason, receipt_key=batch.receipt_key, published_at=batch.append.published_at)
    try:
        checkpoint_uuid = UUID(batch.append.checkpoint_id)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("V4 TITLE no-SDK append lost its minted checkpoint identity") from error
    if checkpoint_uuid.version != 4 or str(checkpoint_uuid) != batch.append.checkpoint_id:
        raise AuditIntegrityError("V4 TITLE no-SDK append lost its minted checkpoint identity")
    append = batch.append
    expected_append = reference.append
    if (batch.receipt_json, batch.receipt_key, batch.receipt_chunk_key) != (
        reference.receipt_json,
        reference.receipt_key,
        reference.receipt_chunk_key,
    ) or (
        append.session_id,
        append.operation_id,
        append.expected_checkpoint_id,
        append.expected_cursor,
        append.snapshot_json,
        append.events,
        append.published_at,
        append.handoff,
        append.predecessor_json,
    ) != (
        expected_append.session_id,
        expected_append.operation_id,
        expected_append.expected_checkpoint_id,
        expected_append.expected_cursor,
        expected_append.snapshot_json,
        expected_append.events,
        expected_append.published_at,
        expected_append.handoff,
        expected_append.predecessor_json,
    ):
        raise AuditIntegrityError("V4 TITLE no-SDK tail changed its complete original bytes")
    expected_chunks = (
        reference.prospective[0],
        *reference.prospective[1:-1],
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if batch.prospective != expected_chunks or sum(len(chunk.payload) for chunk in batch.prospective) > source.reserved_bytes:
        raise AuditIntegrityError("V4 TITLE no-SDK tail exceeded its original hold or changed immutable bytes")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )
    scope = _PaidTailScope(conn, transaction, source, plan, batch)
    conn.info[_SCOPE_KEY] = scope
    try:
        yield
        expected = StoredContinuationSnapshot(
            batch.append.snapshot_json, batch.append.expected_cursor + len(batch.append.events), batch.append.checkpoint_id
        )
        if scope.stage != 3 or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != expected:
            raise AuditIntegrityError("V4 TITLE no-SDK tail did not consume its complete batch and hold")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("V4 TITLE no-SDK tail lost its original transaction scope")


@contextmanager
def admit_v4_title_paid_incomplete_tail_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    paid_plan: TitlePaidOverflowTailPlan,
    plan: TitlePaidIncompleteTailPlan,
    no_sdk_plan: TitleNoSdkTailPlan,
    batch: TitlePaidIncompleteTailBatch,
) -> Iterator[None]:
    """Credit only the original returned-incomplete TITLE terminal batch."""
    if (
        type(source) is not ProviderTerminalHoldSource
        or type(paid_plan) is not TitlePaidOverflowTailPlan
        or type(plan) is not TitlePaidIncompleteTailPlan
        or type(no_sdk_plan) is not TitleNoSdkTailPlan
        or type(batch) is not TitlePaidIncompleteTailBatch
        or _SCOPE_KEY in conn.info
    ):
        raise AuditIntegrityError("V4 TITLE paid-incomplete tail requires one original closed scope")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("V4 TITLE paid-incomplete tail requires its original root transaction")
    root = decode_continuation_snapshot(plan.admitted.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    reserved, contract = title_combined_terminal_hold_contract(paid_plan, plan, no_sdk_plan)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or (source.session_id, source.operation_id) != (plan.identity.session_id, plan.identity.operation_id)
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
        or source.reserved_bytes != reserved
        or source.fallback_contract_sha256 != contract
        or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != plan.admitted
        or prepare_title_paid_incomplete_tail_plan(identity=plan.identity, admitted=plan.admitted, creator_sequence=plan.creator_sequence)
        != plan
    ):
        raise AuditIntegrityError("V4 TITLE paid-incomplete tail lost original hold, policy or admitted head")
    batch.append.require_original()
    receipt = decode_paid_incomplete_receipt(batch.receipt_json, expected=plan.identity)
    reference = build_title_paid_incomplete_tail_batch(plan, receipt=receipt, published_at=batch.append.published_at)
    try:
        checkpoint_uuid = UUID(batch.append.checkpoint_id)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("V4 TITLE paid-incomplete append lost its minted checkpoint identity") from error
    if checkpoint_uuid.version != 4 or str(checkpoint_uuid) != batch.append.checkpoint_id:
        raise AuditIntegrityError("V4 TITLE paid-incomplete append lost its minted checkpoint identity")
    append = batch.append
    expected = reference.append
    if (batch.receipt_json, batch.receipt_key, batch.receipt_chunk_key) != (
        reference.receipt_json,
        reference.receipt_key,
        reference.receipt_chunk_key,
    ) or (
        append.session_id,
        append.operation_id,
        append.expected_checkpoint_id,
        append.expected_cursor,
        append.snapshot_json,
        append.events,
        append.published_at,
        append.handoff,
        append.predecessor_json,
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
        raise AuditIntegrityError("V4 TITLE paid-incomplete tail changed complete original bytes")
    expected_chunks = (
        reference.prospective[0],
        *reference.prospective[1:-1],
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if batch.prospective != expected_chunks or sum(len(chunk.payload) for chunk in batch.prospective) > source.reserved_bytes:
        raise AuditIntegrityError("V4 TITLE paid-incomplete tail exceeded original hold or changed immutable bytes")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )
    scope = _PaidTailScope(conn, transaction, source, plan, batch)
    conn.info[_SCOPE_KEY] = scope
    try:
        yield
        retained = StoredContinuationSnapshot(append.snapshot_json, append.expected_cursor + len(append.events), append.checkpoint_id)
        if scope.stage != 3 or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != retained:
            raise AuditIntegrityError("V4 TITLE paid-incomplete tail did not consume complete batch and hold")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("V4 TITLE paid-incomplete tail lost original transaction scope")

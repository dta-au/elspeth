"""Common public shape for a cold-proved returned paid result with missing evidence.

Only a family-specific cold reader can establish original admission, exact
terminal journal, quota, and consumed H. This carrier and public projector do
not supply those proofs or grant a response, retry, graph, tool, or handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_incomplete import (
    PaidIncompleteReceipt,
    decode_paid_incomplete_receipt,
    paid_incomplete_receipt_keys,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical


@dataclass(frozen=True, slots=True)
class PaidIncompleteReceiptEvidence:
    """Common cold receipt/physical/H facts, short of a family terminal proof."""

    source: ProviderTerminalHoldSource
    receipt: PaidIncompleteReceipt
    provider_attempt_id: str
    hold_state: Literal["active", "consumed"]


def read_paid_incomplete_receipt_evidence_on_connection(
    conn: Connection, *, session_id: str, operation_id: str, effect_key: str
) -> PaidIncompleteReceiptEvidence | None:
    """Derive one distinct receipt from SQL; the family must still prove its tail.

    Returns None for a different disposition. It never accepts a caller's
    receipt, source, physical attempt, or SDK-return assertion as proof.
    """
    row = conn.execute(
        select(holds).where(holds.c.session_id == session_id, holds.c.operation_id == operation_id, holds.c.effect_key == effect_key)
    ).one_or_none()
    if row is None:
        return None
    source = ProviderTerminalHoldSource(
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
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key
        )
    ).one_or_none()
    if effect is None:
        raise AuditIntegrityError("paid incomplete hold lost its original provider effect")
    if effect.disposition != "paid_incomplete":
        return None
    if (
        effect.owner != "provider"
        or effect.input_sha256 != source.source_input_sha256
        or effect.provider_attempt_id is None
        or effect.requested_model is None
        or effect.response_finished_at is None
        or effect.receipt_key is None
        or effect.result_chunk_key is None
        or hold.provider_attempt_id != effect.provider_attempt_id
    ):
        raise AuditIntegrityError("paid incomplete receipt lost its original effect and physical attempt")
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    if (
        attempt is None
        or attempt.session_id != session_id
        or attempt.operation_id != source.session_operation_id
        or attempt.operation_epoch != source.session_operation_epoch
        or sha256(attempt.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or attempt.ledger_entry_id is None
        or attempt.settled_at is None
    ):
        raise AuditIntegrityError("paid incomplete receipt lost its settled original physical attempt")
    identity = PaidOverflowIdentity(
        session_id=session_id,
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
    chunk = read_bounded_retained_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=effect.result_chunk_key, allowance=source.allowance_bytes
    )
    if chunk is None or chunk.codec != "composer.paid-incomplete.v1":
        raise AuditIntegrityError("paid incomplete receipt lost its distinct bounded immutable chunk")
    receipt_json = chunk.payload.decode("utf-8", errors="strict")
    receipt = decode_paid_incomplete_receipt(receipt_json, expected=identity)
    receipt_key, chunk_key = paid_incomplete_receipt_keys(receipt_json, expected=identity)
    if effect.receipt_key != receipt_key or effect.result_chunk_key != chunk_key:
        raise AuditIntegrityError("paid incomplete receipt changed its exact original digest keys")
    return PaidIncompleteReceiptEvidence(source, receipt, effect.provider_attempt_id, hold.state)


@dataclass(frozen=True, slots=True)
class VerifiedPaidIncompleteTerminal:
    """Family cold-reader result after atomic terminal and H consumption."""

    source: ProviderTerminalHoldSource
    receipt: PaidIncompleteReceipt
    terminal: StoredContinuationSnapshot
    provider_attempt_id: str
    hold_state: Literal["consumed"]

    def __post_init__(self) -> None:
        if (
            type(self.source) is not ProviderTerminalHoldSource
            or type(self.receipt) is not PaidIncompleteReceipt
            or type(self.terminal) is not StoredContinuationSnapshot
            or type(self.provider_attempt_id) is not str
            or not self.provider_attempt_id
            or self.hold_state != "consumed"
        ):
            raise AuditIntegrityError("paid incomplete terminal lacks its exact cold-proved carrier")
        identity = self.receipt.identity
        if (
            identity.session_id != self.source.session_id
            or identity.operation_id != self.source.operation_id
            or identity.effect_key != self.source.effect_key
            or identity.attempt_id != self.provider_attempt_id
            or identity.session_operation_id != self.source.session_operation_id
            or identity.session_operation_epoch != self.source.session_operation_epoch
            or identity.ownership_generation != self.source.creator_generation
            or identity.lease_sha256 != self.source.lease_sha256
            or identity.policy_sha256 != self.source.policy_sha256
            or identity.source_checkpoint_id != self.source.source_checkpoint_id
            or identity.source_cursor != self.source.source_cursor
            or identity.source_snapshot_sha256 != self.source.source_snapshot_sha256
        ):
            raise AuditIntegrityError("paid incomplete terminal changed its original provider hold identity")


def paid_incomplete_error(request_id: str | None, cost: PaidOverflowCost) -> ComposerOperationError:
    """Static public 507 error; never disclose provider fields or losses."""
    if type(cost) is not PaidOverflowCost:
        raise TypeError("paid incomplete public error requires its bounded cost observation")
    return ComposerOperationError(
        http_status=507,
        failure_code="http_error",
        error_type="paid_result_incomplete",
        body={
            "error_type": "paid_result_incomplete",
            "detail": "The returned paid result could not be fully retained. It was not used or retried.",
            "evidence_status": "incomplete",
            "retryable": False,
            "cost": {"status": cost.status, "amount": cost.amount},
            "request_id": request_id,
        },
        diagnostic_id=None,
    )

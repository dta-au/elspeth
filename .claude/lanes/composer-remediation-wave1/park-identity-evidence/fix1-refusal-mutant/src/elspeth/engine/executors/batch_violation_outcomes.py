"""FAILED-first terminal outcomes for a batch seam's Tier-1 violation.

A batch-aware transform's flush (an aggregation or a collector) can raise a
Tier-1 violation that belongs to the whole batch: the declaration cross-check,
a quarantine contradiction, a pass-through shape error, or a required input
field the build proved present missing from a buffered row (ADR-013
Amendment 2026-09-27). The run then ends, but no buffered token may be left
without a terminal outcome: every one is recorded ``FAILURE``/``UNROUTED``
with the violation's value-free audit dict BEFORE the violation propagates.

This is the one recorder both seams call, so the error hash and the per-token
audit payload are the same whichever seam raised.
"""

from __future__ import annotations

from collections.abc import Sequence

from elspeth.contracts import TokenInfo
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.declaration_contracts import AggregateDeclarationContractViolation, DeclarationContractViolation
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import (
    AuditIntegrityError,
    BatchDeclaredInputFieldsViolation,
    BatchPassthroughShapeError,
    BatchQuarantineContradictionError,
    PassThroughContractViolation,
    PluginContractViolation,
)
from elspeth.contracts.types import NodeID
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.engine._error_hash import compute_error_hash

type BatchSeamViolation = (
    DeclarationContractViolation
    | PluginContractViolation
    | AggregateDeclarationContractViolation
    | BatchQuarantineContradictionError
    | BatchPassthroughShapeError
    | BatchDeclaredInputFieldsViolation
)


def record_batch_violation_failures(
    data_flow: DataFlowRepository,
    *,
    coordination_token: CoordinationToken,
    run_id: str,
    tokens: Sequence[TokenInfo],
    violation: BatchSeamViolation,
    transform_name: str,
    node_id: NodeID,
    triggering_token_id: str | None,
) -> None:
    """Record ``FAILURE``/``UNROUTED`` for every buffered token of a batch whose flush raised ``violation``.

    The violation is batch-level, but the audit trail carries per-token
    evidence: each token's context is the violation's audit dict plus its own
    ``token_id``/``row_id`` and the token that triggered the flush (None for a
    timeout, end-of-source or end-of-group flush).

    Raises:
        AuditIntegrityError: a terminal write failed part-way, so some buffered
            tokens may carry FAILED records and others none. The original
            violation stays on ``__context__`` when the caller is handling it.
    """
    if isinstance(violation, PassThroughContractViolation):
        violation_summary = f"PassThroughContractViolation:{transform_name}:{sorted(violation.divergence_set)}"
    else:
        violation_summary = f"{type(violation).__name__}:{transform_name}"
    error_hash = compute_error_hash(violation_summary)
    base_audit = violation.to_audit_dict()

    for token in tokens:
        per_token_audit_payload: dict[str, object] = {
            **base_audit,
            "token_id": token.token_id,
            "row_id": token.row_id,
            "triggering_token_id": triggering_token_id,
        }
        try:
            data_flow.record_token_outcome_leader(
                coordination_token=coordination_token,
                ref=TokenRef(token_id=token.token_id, run_id=run_id),
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.UNROUTED,
                error_hash=error_hash,
                context=per_token_audit_payload,
            )
        except LandscapeRecordError as record_failure:
            raise AuditIntegrityError(
                f"Failed to record {type(violation).__name__} FAILED outcome "
                f"for token {token.token_id!r} in batch flush "
                f"(transform={transform_name!r}, node={node_id!r}). "
                f"Audit trail is INCOMPLETE — FAILED records may exist for some "
                f"buffered tokens but not others. "
                f"Recorder failure: {type(record_failure).__name__}: {record_failure}. "
                f"Original violation: {violation!s}"
            ) from record_failure

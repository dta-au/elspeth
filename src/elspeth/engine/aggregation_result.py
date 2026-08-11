"""Shared validation and terminal-disposition derivation for aggregation results."""

from __future__ import annotations

from collections.abc import Sequence

from elspeth.contracts import AggregationParentDisposition, TokenInfo, TransformResult
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.engine._error_hash import compute_error_hash


def validated_quarantined_indices(
    result: TransformResult,
    *,
    buffered_token_count: int,
    aggregation_name: str,
) -> set[int]:
    """Extract and validate batch-transform quarantine metadata."""
    if result.success_reason is None or "metadata" not in result.success_reason:
        return set()

    metadata = result.success_reason["metadata"]
    if type(metadata) is not dict:
        raise OrchestrationInvariantError(
            f"Aggregation {aggregation_name!r} returned success_reason.metadata={metadata!r}; "
            "expected dict when quarantine metadata is present"
        )
    if "quarantined_indices" not in metadata:
        return set()

    raw_indices = metadata["quarantined_indices"]
    if type(raw_indices) is not list:
        raise OrchestrationInvariantError(
            f"Aggregation {aggregation_name!r} returned quarantined_indices={raw_indices!r}; expected list[int]"
        )

    quarantined_index_set: set[int] = set()
    for position, raw_index in enumerate(raw_indices):
        if type(raw_index) is not int:
            raise OrchestrationInvariantError(
                f"Aggregation {aggregation_name!r} returned quarantined_indices[{position}]={raw_index!r}; expected int"
            )
        if raw_index < 0 or raw_index >= buffered_token_count:
            raise OrchestrationInvariantError(
                f"Aggregation {aggregation_name!r} returned quarantined_indices[{position}]={raw_index}; "
                f"valid index range is 0..{buffered_token_count - 1}"
            )
        quarantined_index_set.add(raw_index)
    return quarantined_index_set


def aggregation_parent_dispositions(
    tokens: Sequence[TokenInfo],
    *,
    run_id: str,
    batch_id: str,
    quarantined_indices: set[int],
) -> tuple[AggregationParentDisposition, ...]:
    """Derive the exact ordered terminal plan for a successful batch."""
    return tuple(
        AggregationParentDisposition(
            parent_ref=TokenRef(token_id=token.token_id, run_id=run_id),
            outcome=TerminalOutcome.FAILURE if index in quarantined_indices else TerminalOutcome.TRANSIENT,
            path=TerminalPath.QUARANTINED_AT_SOURCE if index in quarantined_indices else TerminalPath.BATCH_CONSUMED,
            error_hash=(compute_error_hash(f"quarantined_in_batch:{batch_id}:{index}") if index in quarantined_indices else None),
        )
        for index, token in enumerate(tokens)
    )

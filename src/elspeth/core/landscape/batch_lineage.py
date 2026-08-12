"""Durable retry lineage for aggregation batches.

A crash-interrupted or FAILED flush is retried on a NEW batch linked to the
dead original through ``batches.retry_of_batch_id`` (one-to-one, same-run,
same-node; see ``BatchRepository.retry_batch``). The members' live BUFFERED
``token_outcomes`` rows deliberately keep the ORIGINAL acceptance batch_id —
audit history is immutable, and duplicate live BUFFERED acceptances are
corruption — so any consumer proving "this member is buffered into this
batch" must accept the whole durable retry chain, not only the completing
batch id.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.schema import batches_table


def batch_retry_lineage_ids(
    execute_fetchone: Callable[[Executable], Row[Any] | None],
    *,
    batch_id: str,
    run_id: str,
    aggregation_node_id: str,
    retry_of_batch_id: str | None,
) -> tuple[str, ...]:
    """Return ``batch_id`` plus every retried ancestor, newest first.

    Every ancestor must exist, belong to the same run, and belong to the same
    aggregation node — a lineage that crosses runs or nodes is audit
    corruption, as is a cycle.

    Args:
        execute_fetchone: Executes one SELECT on the caller's connection (the
            receipt writer passes its locked transaction; restore readers pass
            their read-only ops seam).
        batch_id: The completing/receipt batch.
        run_id: The batch's run.
        aggregation_node_id: The batch's aggregation node.
        retry_of_batch_id: The completing batch's direct retry parent
            (``None`` for a first-attempt batch).

    Raises:
        AuditIntegrityError: On a missing, foreign, wrong-node, or cyclic
            ancestor.
    """
    lineage = [batch_id]
    ancestor_id = retry_of_batch_id
    while ancestor_id is not None:
        if ancestor_id in lineage:
            raise AuditIntegrityError(
                f"aggregation batch {batch_id!r} has a cyclic retry lineage at ancestor {ancestor_id!r} — audit corruption"
            )
        ancestor = execute_fetchone(
            select(
                batches_table.c.run_id,
                batches_table.c.aggregation_node_id,
                batches_table.c.retry_of_batch_id,
            ).where(batches_table.c.batch_id == ancestor_id)
        )
        if ancestor is None or ancestor.run_id != run_id or ancestor.aggregation_node_id != aggregation_node_id:
            raise AuditIntegrityError(
                f"aggregation batch {batch_id!r} has a missing, foreign, or wrong-node retry ancestor {ancestor_id!r}"
            )
        lineage.append(ancestor_id)
        ancestor_id = ancestor.retry_of_batch_id
    return tuple(lineage)

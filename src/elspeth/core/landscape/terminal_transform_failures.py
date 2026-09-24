"""The transform error that decided each terminally failed token.

A ``transform_errors`` row is evidence of one ATTEMPT, not an outcome. The
table has no ``(token_id, transform_id)`` uniqueness, and an error write that
committed before a crash stays when the resumed attempt runs again. That
attempt can fail again and write a second row. It can also SUCCEED: the batch
is consumed, or the row continues and is delivered. Counting rows, or even
distinct tokens over rows, therefore reports a delivered row as failed
(operator ruling 2026-09-23, elspeth-5887fb7928: failure and discard COUNTS
derive from terminal outcomes; failed attempts stay queryable as attempt
evidence and never inflate them).

:func:`deciding_transform_errors` is the one definition every counting reader
uses (web discard summary, web failure categories, MCP run summary and error
analysis). A token contributes exactly one row, the error that decided it,
when all of these hold:

1. It has a ``token_outcomes`` row on one of the two paths a transform error
   disposes a token onto: ``QUARANTINED_AT_SOURCE`` (``on_error: discard``,
   per row and per batch since B3) or ``ON_ERROR_ROUTED`` (a named on_error
   sink). ADR-019 pairs each of them only with ``TerminalOutcome.FAILURE``
   and both are terminal (``contracts/enums.py`` ``_LEGAL_TERMINAL_PAIRS``),
   so the path alone says the token terminally failed. A token whose retry
   succeeded is SUCCESS or TRANSIENT and contributes nothing. So does one
   whose later failure happened somewhere a transform error did not decide
   (a sink discard, an unrouted crash, a gate error).
2. The row is the token's LATEST ``transform_errors`` row: greatest
   ``created_at``, with ``error_id`` breaking a tie so exactly one row is
   picked. Terminal outcomes carry no node, and members of a failed batch have
   no ``node_states`` row at the aggregation (only the flush's triggering
   token does), so ``transform_errors`` is the only per-member, per-node record
   there is. After the terminal decision nothing writes another error for the
   token. So the latest row names the node that decided it and carries that
   attempt's category. A row from an earlier attempt at the same node, or at
   an upstream node the token later got past, is attempt evidence only.
3. The token never COMPLETED a node state at that row's node. A completed
   state there proves a later attempt got the token past the node. The latest
   row alone cannot see that when the terminal FAILURE came from a path that
   writes no transform error, such as a collector or batch member quarantine.

Ordering by ``created_at`` compares wall clocks across a crash and its
resume. Those are separate process lifetimes, normally seconds to hours
apart, so the order holds unless the resuming host's clock is behind the
crashed host's by more than that gap.

The latest row is found by ranking the runs' error rows in ONE pass (a
``row_number()`` window over ``(run_id, token_id)``), not by a correlated
"latest row for this token" subquery per error row. The correlated form
filters on ``run_id`` and ``token_id``, and ``transform_errors`` indexes each
column on its own. An audit database carries no ANALYZE statistics, so SQLite
prices the two single-column candidates the same and takes whichever index
was created last. ``create_all`` creates a table's indexes in set order,
which differs from process to process. On a database where
``ix_transform_errors_run`` came last, every error row rescanned and sorted
the whole run's errors: 14.4s for 10k failed tokens, against 0.02s on a
database with the other order. It is the trap ``ix_token_outcomes_run_token``
records in ``schema.py`` (elspeth-c675c8c2d9). The window reads the run's
errors once, whichever order the indexes were created in.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, and_, exists, func, select

from elspeth.contracts import NodeStateStatus
from elspeth.contracts.enums import TerminalPath
from elspeth.core.landscape.schema import node_states_table, token_outcomes_table, transform_errors_table

TRANSFORM_ERROR_TERMINAL_PATHS: tuple[TerminalPath, ...] = (
    TerminalPath.QUARANTINED_AT_SOURCE,
    TerminalPath.ON_ERROR_ROUTED,
)
"""The terminal paths a transform error disposes a token onto (discard, or a named on_error sink)."""


def deciding_transform_errors(run_ids: Sequence[str]) -> Select[tuple[str, str, str, str, str | None]]:
    """Select one row per terminally failed token: the transform error that decided it.

    Columns: ``run_id``, ``token_id``, ``transform_id`` (the deciding node),
    ``destination`` (``"discard"`` or the on_error target) and
    ``error_details_json``. See the module docstring for the three conditions.
    """
    errors = transform_errors_table
    # Rank 1 is the token's latest error row. The window partitions every
    # error row of the runs, not only the rows the outer filters keep, so a
    # superseded row can never rank first because the latest row was filtered.
    ranked = (
        select(
            errors.c.run_id,
            errors.c.token_id,
            errors.c.transform_id,
            errors.c.destination,
            errors.c.error_details_json,
            func.row_number()
            .over(
                partition_by=(errors.c.run_id, errors.c.token_id),
                order_by=(errors.c.created_at.desc(), errors.c.error_id.desc()),
            )
            .label("recency_rank"),
        )
        .where(errors.c.run_id.in_(run_ids))
        .subquery("ranked_attempts")
    )
    completed_at_node = exists().where(
        node_states_table.c.run_id == ranked.c.run_id,
        node_states_table.c.token_id == ranked.c.token_id,
        node_states_table.c.node_id == ranked.c.transform_id,
        node_states_table.c.status == NodeStateStatus.COMPLETED,
    )
    return (
        select(
            ranked.c.run_id,
            ranked.c.token_id,
            ranked.c.transform_id,
            ranked.c.destination,
            ranked.c.error_details_json,
        )
        .select_from(
            ranked.join(
                token_outcomes_table,
                and_(
                    token_outcomes_table.c.run_id == ranked.c.run_id,
                    token_outcomes_table.c.token_id == ranked.c.token_id,
                ),
            )
        )
        .where(ranked.c.recency_rank == 1)
        .where(token_outcomes_table.c.path.in_(TRANSFORM_ERROR_TERMINAL_PATHS))
        .where(~completed_at_node)
    )

"""The one counting authority for failed tokens and failed collector groups.

It defines three quantities, and every counting reader (web run accounting and
run status, web failure categories, the web discard summary, the MCP run
summary and error analysis) takes them from here, never from its own query:

- **Transform-decided failed tokens** (:func:`deciding_transform_errors`):
  tokens whose terminal outcome is a failure a transform error decided.
- **Collector member tokens, M** (:func:`deciding_collector_group_failures`):
  tokens whose collector group FAILED as a whole, each counted once under the
  reason its group records.
- **Failed collector groups, G** (:func:`failed_collector_groups`): one per
  group verdict, including groups no member reached (a ``require_all`` roster
  whose members were all lost, or a zero-member ``empty_expansion`` group).

M and G are different units: G counts groups, M counts tokens. A zero-member
group is in G only, and G is never added to a token total.

The first two are disjoint by terminal path. The transform arm admits only
``QUARANTINED_AT_SOURCE`` and ``ON_ERROR_ROUTED``; a failed collector group's
members all end ``(FAILURE, UNROUTED)`` (every arm, measured: missing members,
returned error, contract violation, nested groups). UNROUTED is NOT the
discriminator, because coalesce, row_union, escalation and unrouted crashes
end tokens on it too. The discriminator is the member's parsed
``CollectorGroupFailure`` hold (:mod:`collector_group_failure_holds`), cross-
checked against its group's ``collector_group_failures`` row. A member that
carries a transform error from an earlier node is counted once, here, because
its terminal path is not one the transform arm admits.

Collector group verdicts cannot be superseded. ``collector_group_failures`` has
primary key ``(run_id, group_id)`` and a second record raises
``AuditIntegrityError`` (``ExecutionRepository.complete_collector_failure``);
member holds complete FAILED only from OPEN, in the verdict's one transaction;
resume completes a recorded verdict without re-invoking the plugin; and
``ix_token_outcomes_terminal_unique`` allows one terminal outcome per token.
What CAN be superseded is the flush: when the plugin raises, the flush state
fails, the holds stay OPEN and resume flushes again. So flush states are
attempt evidence and this module never reads them; only a
``CollectorGroupFailure`` hold on a token that ended UNROUTED counts.

Between a committed verdict and its members' terminal outcomes (a crash in
that window), G is 1 and M is 0 until resume writes the terminals. That is
the terminal-outcome ruling applied, not a gap: no token has terminally
failed yet.

Coalesce and row_union group failures have the same whole-group shape and are
NOT counted by name here. Their members fail on their own recorded reasons
and are visible only through terminal outcomes and group losses (a named
gap: the operator ruling covers collectors).

The transform arm follows.

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

The collector member arm has the same kind of trap. ``node_states`` has no
index that leads with ``run_id``, and node ids are stable across runs of the
same pipeline (a hash of the node's configuration), so a collector node id
matches that node's states in EVERY run the database holds. A query that puts
the collector node id in SQL lets SQLite drive ``node_states`` through
``ix_node_states_node`` and scan the node's whole history. So the member query
drives from the runs' ``(FAILURE, UNROUTED)`` terminal outcomes and reaches
``node_states`` by ``token_id`` only; the collector nodes are applied to the
fetched rows. A failed token has a handful of FAILED states, so the rows read
are bounded by the runs' failed tokens, never by history.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, and_, exists, func, select
from sqlalchemy.engine import Connection

from elspeth.contracts import NodeStateStatus
from elspeth.contracts.enums import CollectorGroupFailureReason, NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.collector_group_failure_holds import parse_collector_group_failure_hold
from elspeth.core.landscape.schema import (
    collector_group_failures_table,
    node_states_table,
    nodes_table,
    token_outcomes_table,
    transform_errors_table,
)

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


COLLECTOR_GROUP_MEMBER_TERMINAL_PATH = TerminalPath.UNROUTED
"""The terminal path every member of a failed collector group ends on (with ``TerminalOutcome.FAILURE``)."""


@dataclass(frozen=True, slots=True)
class CollectorGroupMemberFailure:
    """One token whose collector group FAILED, under the reason the group's verdict records."""

    run_id: str
    token_id: str
    collector_node_id: str
    group_id: str
    failure_reason: CollectorGroupFailureReason


def failed_collector_groups(run_ids: Sequence[str]) -> Select[tuple[str, str, str, str]]:
    """Select one row per failed collector group of the runs: the group verdict.

    Columns: ``run_id``, ``group_id``, ``collector_node_id`` and
    ``failure_reason`` (a :class:`CollectorGroupFailureReason` value, held to
    that vocabulary by a CHECK). This is the only source of G, the count of
    failed groups. It includes a group no member reached.
    """
    groups = collector_group_failures_table
    return select(groups.c.run_id, groups.c.group_id, groups.c.collector_node_id, groups.c.failure_reason).where(
        groups.c.run_id.in_(run_ids)
    )


def _recorded_reason(value: str, *, run_id: str, group_id: str) -> CollectorGroupFailureReason:
    """Parse a group row's ``failure_reason``. The CHECK admits only the vocabulary, so a miss is corruption."""
    try:
        return CollectorGroupFailureReason(value)
    except ValueError as exc:
        raise AuditIntegrityError(
            f"collector_group_failures row for group {group_id!r} (run {run_id!r}) records a failure_reason outside "
            "the CollectorGroupFailureReason vocabulary"
        ) from exc


def deciding_collector_group_failures(conn: Connection, run_ids: Sequence[str]) -> tuple[CollectorGroupMemberFailure, ...]:
    """One entry per token of the runs whose collector group FAILED: M, the member-token count.

    A token counts when it ended ``(FAILURE, UNROUTED)`` and holds a FAILED
    state at one of its run's collector nodes whose error is a
    ``CollectorGroupFailure`` hold. The hold names its group, and it must
    agree with that group's ``collector_group_failures`` row on the collector
    node and the reason. Any other FAILED state (a flush state, a quarantined
    member of a successful flush, a failed attempt at another node) is not a
    group verdict and does not count. See the module docstring for why the
    query never names a node id.

    Raises:
        AuditIntegrityError: A hold that is malformed, that names a group
            with no verdict row, that disagrees with its group's row on the
            node or the reason, or a token holding more than one verdict.
    """
    groups: dict[tuple[str, str], tuple[str, CollectorGroupFailureReason]] = {
        (str(row.run_id), str(row.group_id)): (
            str(row.collector_node_id),
            _recorded_reason(str(row.failure_reason), run_id=str(row.run_id), group_id=str(row.group_id)),
        )
        for row in conn.execute(failed_collector_groups(run_ids))
    }
    collector_nodes = {
        (str(row.run_id), str(row.node_id))
        for row in conn.execute(
            select(nodes_table.c.run_id, nodes_table.c.node_id)
            .where(nodes_table.c.run_id.in_(run_ids))
            .where(nodes_table.c.node_type == NodeType.COLLECTOR.value)
        )
    }
    if not collector_nodes:
        # Exactly what the loop below would return: it keeps only states at
        # a collector node.
        return ()
    failed_states = (
        select(token_outcomes_table.c.run_id, token_outcomes_table.c.token_id, node_states_table.c.node_id, node_states_table.c.error_json)
        .select_from(
            token_outcomes_table.join(
                node_states_table,
                and_(
                    node_states_table.c.token_id == token_outcomes_table.c.token_id,
                    node_states_table.c.run_id == token_outcomes_table.c.run_id,
                ),
            )
        )
        .where(token_outcomes_table.c.run_id.in_(run_ids))
        .where(token_outcomes_table.c.completed == 1)
        .where(token_outcomes_table.c.outcome == TerminalOutcome.FAILURE.value)
        .where(token_outcomes_table.c.path == COLLECTOR_GROUP_MEMBER_TERMINAL_PATH.value)
        .where(node_states_table.c.status == NodeStateStatus.FAILED.value)
    )
    members: dict[tuple[str, str], CollectorGroupMemberFailure] = {}
    for row in conn.execute(failed_states):
        run_id, token_id, node_id = str(row.run_id), str(row.token_id), str(row.node_id)
        if (run_id, node_id) not in collector_nodes:
            continue
        hold = parse_collector_group_failure_hold(token_id, node_id, row.error_json)
        if hold is None:
            continue
        if (run_id, token_id) in members:
            raise AuditIntegrityError(f"Token {token_id!r} (run {run_id!r}) holds more than one collector group-failure verdict")
        if (run_id, hold.group_id) not in groups:
            raise AuditIntegrityError(
                f"Collector group-failure hold of token {token_id!r} at {node_id!r} names group {hold.group_id!r}, "
                f"which has no collector_group_failures row in run {run_id!r}"
            )
        recorded_node, recorded_reason = groups[(run_id, hold.group_id)]
        if (recorded_node, recorded_reason) != (node_id, hold.failure_reason):
            raise AuditIntegrityError(
                f"Collector group-failure hold of token {token_id!r} at {node_id!r} records {hold.failure_reason.value!r} "
                f"for group {hold.group_id!r}, whose verdict records {recorded_reason.value!r} at {recorded_node!r}"
            )
        members[(run_id, token_id)] = CollectorGroupMemberFailure(
            run_id=run_id,
            token_id=token_id,
            collector_node_id=node_id,
            group_id=hold.group_id,
            failure_reason=hold.failure_reason,
        )
    return tuple(members.values())

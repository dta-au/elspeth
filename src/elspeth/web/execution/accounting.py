"""Landscape-derived run accounting for the web execution API."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import and_, case, func, or_, select

from elspeth.contracts.audit import _TERMINAL_PAIR_FIELD_CONSTRAINTS, DISCARD_SINK_NAME
from elspeth.contracts.enums import _LEGAL_TERMINAL_PAIRS, _NON_TERMINAL_PATHS, TerminalOutcome, TerminalPath
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import (
    rows_table,
    run_sources_table,
    runs_table,
    token_outcomes_table,
    tokens_table,
    validation_errors_table,
)
from elspeth.web.config import WebSettings
from elspeth.web.execution.discard_summary import DISCARD_DESTINATION, _sqlite_database_file_missing, _unique_run_ids
from elspeth.web.execution.schemas import (
    RunAccounting,
    RunAccountingCorruption,
    RunAccountingIntegrity,
    RunAccountingRouting,
    RunAccountingSource,
    RunAccountingTokens,
)

# Bound on violation strings carried per corrupt run. Violations beyond the
# cap collapse into a single "+N more" marker: the corpus can be as large as
# the run's distinct outcome shapes, and the marker keeps the response bounded
# without pretending the list is complete.
_MAX_VIOLATIONS_PER_RUN = 8


@dataclass(frozen=True)
class RunAccountingBatch:
    """Per-run accounting results for one batch read.

    ``accounting`` carries runs whose recorded token outcomes satisfy the
    canonical ADR-019 contract; ``corrupt`` carries an explicit
    :class:`RunAccountingCorruption` for each run that does not. The split is
    the point (elspeth-d5578ccd98): integrity failure is a PER-RUN fact, and
    representing it as a batch-level exception let one corrupt historical run
    hide accounting for every healthy run in a session history response.
    """

    accounting: Mapping[str, RunAccounting] = field(default_factory=dict)
    corrupt: Mapping[str, RunAccountingCorruption] = field(default_factory=dict)


def load_run_accounting_for_settings(
    settings: WebSettings,
    landscape_run_ids: Iterable[str | None],
) -> RunAccountingBatch:
    """Load run accounting from the configured Landscape database."""
    run_ids = _unique_run_ids(landscape_run_ids)
    if not run_ids:
        return RunAccountingBatch()

    landscape_url = settings.get_landscape_url()
    if _sqlite_database_file_missing(landscape_url):
        return RunAccountingBatch()

    with LandscapeDB.from_url(
        landscape_url,
        passphrase=settings.landscape_passphrase,
        create_tables=False,
        read_only=True,
    ) as db:
        return load_run_accounting_map_from_db(db, run_ids)


# The shape census below reads actual VALUES only for ``sink_name`` — the one
# evidence column any ``exact`` constraint pins today — and every other
# evidence column at presence (NULL / non-NULL) granularity, which is all the
# ``required`` / ``forbidden`` constraints inspect. If a future constraint
# pins an exact value on a different column, the census must start carrying
# that column's value; fail at import rather than silently passing it.
_EXACT_CONSTRAINT_FIELDS = {name for constraints in _TERMINAL_PAIR_FIELD_CONSTRAINTS.values() for name in constraints.exact}
if not {"sink_name"} >= _EXACT_CONSTRAINT_FIELDS:
    raise AssertionError(
        f"_TERMINAL_PAIR_FIELD_CONSTRAINTS pins exact values on {sorted(_EXACT_CONSTRAINT_FIELDS - {'sink_name'})} — "
        "the accounting shape census only carries sink_name values and must be extended before this constraint can be enforced."
    )


def _outcome_shape_violation(
    *,
    completed: int,
    outcome: str | None,
    path: str,
    sink_name: str | None,
    evidence_present: dict[str, bool],
    count: int,
) -> str | None:
    """Validate one distinct token-outcome shape against the ADR-019 contract.

    The read model must not trust stored aggregates: an unknown enum string,
    an illegal (outcome, path) pair, or missing pair evidence can be
    count-complete and would previously satisfy ``closure == "closed"``.
    Validation runs at GROUP BY granularity — one check per distinct shape,
    never per row — so a large run costs the same as a small one.

    Completed rows get the full contract (enum, legal pair, pair-field
    evidence). Non-completed rows get exactly what closure classification
    trusts: the ADR-019 XOR (outcome must be NULL) and a non-terminal path.

    Returns one bounded, audit-safe violation string, or None. Text carries
    enum values, column names, and counts — sink_name is an authored node
    name (same disclosure class as node_id) — never row payloads.
    """
    if completed not in (0, 1):
        return f"{count} outcome row(s) with non-boolean completed={completed!r}"

    try:
        parsed_path = TerminalPath(path)
    except ValueError:
        return f"{count} outcome row(s) with unknown path {path!r}"

    if completed == 0:
        if outcome is not None:
            return f"{count} outcome row(s) with completed=0 but outcome={outcome!r} (ADR-019 XOR violation)"
        if parsed_path not in _NON_TERMINAL_PATHS:
            return f"{count} outcome row(s) with completed=0 on terminal path {path!r}"
        return None

    if outcome is None:
        return f"{count} outcome row(s) with completed=1 but outcome NULL (ADR-019 XOR violation)"
    try:
        parsed_outcome = TerminalOutcome(outcome)
    except ValueError:
        return f"{count} outcome row(s) with unknown outcome {outcome!r}"
    if (parsed_outcome, parsed_path) not in _LEGAL_TERMINAL_PAIRS:
        return f"{count} outcome row(s) with ({outcome!r}, {path!r}) — not a legal ADR-019 terminal pair"

    constraints = _TERMINAL_PAIR_FIELD_CONSTRAINTS[(parsed_outcome, parsed_path)]
    for field_name in constraints.required:
        if not evidence_present[field_name]:
            return f"{count} outcome row(s) with ({outcome!r}, {path!r}) missing required {field_name}"
    for field_name, expected in constraints.exact.items():
        # Import-time guard above: exact constraints only name sink_name.
        if sink_name != expected:
            return f"{count} outcome row(s) with ({outcome!r}, {path!r}) requiring {field_name}={expected!r}, got {sink_name!r}"
    for field_name in constraints.forbidden:
        if evidence_present[field_name]:
            return f"{count} outcome row(s) with ({outcome!r}, {path!r}) — pair forbids {field_name}"
    return None


def _bounded_violations(violations: list[str]) -> list[str]:
    if len(violations) <= _MAX_VIOLATIONS_PER_RUN:
        return violations
    kept = violations[:_MAX_VIOLATIONS_PER_RUN]
    kept.append(f"+{len(violations) - _MAX_VIOLATIONS_PER_RUN} more violation(s) truncated")
    return kept


def load_run_accounting_map_from_db(
    db: LandscapeDB,
    landscape_run_ids: Iterable[str],
) -> RunAccountingBatch:
    """Derive accounting for multiple Landscape runs from an open database.

    Runs whose recorded outcomes violate the canonical contract are returned
    in ``corrupt`` instead of raising, so one corrupt run cannot hide
    accounting for healthy runs requested in the same batch.
    """
    run_ids = _unique_run_ids(landscape_run_ids)
    if not run_ids:
        return RunAccountingBatch()

    present_run_ids: tuple[str, ...] = ()

    with db.read_only_connection() as conn:
        present_stmt = select(runs_table.c.run_id).where(runs_table.c.run_id.in_(run_ids)).order_by(runs_table.c.run_id.asc())
        present_run_ids = tuple(str(row.run_id) for row in conn.execute(present_stmt))
        if not present_run_ids:
            return RunAccountingBatch()

        # Canonical-contract census (elspeth-d5578ccd98): revalidate the
        # stored outcome shapes BEFORE any count is trusted. Grouped at shape
        # granularity — distinct (completed, outcome, path, sink_name,
        # evidence-presence) tuples — so validation cost scales with shape
        # variety, not row count.
        violations_by_run: dict[str, list[str]] = {run_id: [] for run_id in present_run_ids}
        _evidence_presence_columns = {
            "batch_id": token_outcomes_table.c.batch_id.isnot(None).label("has_batch_id"),
            "fork_group_id": token_outcomes_table.c.fork_group_id.isnot(None).label("has_fork_group_id"),
            "join_group_id": token_outcomes_table.c.join_group_id.isnot(None).label("has_join_group_id"),
            "expand_group_id": token_outcomes_table.c.expand_group_id.isnot(None).label("has_expand_group_id"),
            "error_hash": token_outcomes_table.c.error_hash.isnot(None).label("has_error_hash"),
        }
        shape_stmt = (
            select(
                token_outcomes_table.c.run_id,
                token_outcomes_table.c.completed,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
                token_outcomes_table.c.sink_name,
                *_evidence_presence_columns.values(),
                func.count().label("count"),
            )
            .where(token_outcomes_table.c.run_id.in_(present_run_ids))
            .group_by(
                token_outcomes_table.c.run_id,
                token_outcomes_table.c.completed,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
                token_outcomes_table.c.sink_name,
                *_evidence_presence_columns.values(),
            )
        )
        for shape in conn.execute(shape_stmt):
            evidence_present = {
                "sink_name": shape.sink_name is not None,
                "batch_id": bool(shape.has_batch_id),
                "fork_group_id": bool(shape.has_fork_group_id),
                "join_group_id": bool(shape.has_join_group_id),
                "expand_group_id": bool(shape.has_expand_group_id),
                "error_hash": bool(shape.has_error_hash),
            }
            violation = _outcome_shape_violation(
                completed=int(shape.completed),
                outcome=shape.outcome,
                path=shape.path,
                sink_name=shape.sink_name,
                evidence_present=evidence_present,
                # _mapping, not attribute access: Row.count is the tuple
                # method, which shadows the labelled aggregate column.
                count=int(shape._mapping["count"]),
            )
            if violation is not None:
                violations_by_run[str(shape.run_id)].append(violation)

        source_rows_by_source: dict[str, dict[str, int]] = {run_id: {} for run_id in present_run_ids}
        emitted_tokens = _zero_counts(present_run_ids)
        terminal_tokens = _zero_counts(present_run_ids)
        succeeded_tokens = _zero_counts(present_run_ids)
        failed_tokens = _zero_counts(present_run_ids)
        structural_tokens = _zero_counts(present_run_ids)
        routed_success = _zero_counts(present_run_ids)
        routed_failure = _zero_counts(present_run_ids)
        quarantined = _zero_counts(present_run_ids)
        discarded = _zero_counts(present_run_ids)
        missing_terminal_outcomes = _zero_counts(present_run_ids)
        duplicate_terminal_outcomes = _zero_counts(present_run_ids)
        abandoned_tokens = _zero_counts(present_run_ids)

        source_name = func.coalesce(run_sources_table.c.source_name, rows_table.c.source_node_id).label("source_name")
        source_stmt = (
            select(
                rows_table.c.run_id,
                source_name,
                func.count().label("count"),
            )
            .select_from(
                rows_table.outerjoin(
                    run_sources_table,
                    and_(
                        run_sources_table.c.run_id == rows_table.c.run_id,
                        run_sources_table.c.source_node_id == rows_table.c.source_node_id,
                    ),
                )
            )
            .where(rows_table.c.run_id.in_(present_run_ids))
            .group_by(rows_table.c.run_id, source_name)
        )
        for run_id, source_name, count in conn.execute(source_stmt):
            source_rows_by_source[str(run_id)][str(source_name)] = int(count)

        # Rows the source DISCARDED at validation (rows, not tokens): the
        # destination='discard' scope is load-bearing — a QUARANTINED row
        # writes a validation_errors entry too (destination = its sink name)
        # but is admitted and already counted in rows_processed above, so an
        # unscoped count would double-count it. transform_errors never feed
        # this number for the same reason (a transform-discarded row was
        # admitted). Mirrors DiscardSummary's validation arm and
        # tutorial_service._count_discarded_rows.
        rejected_rows_by_source: dict[str, dict[str, int]] = {run_id: {} for run_id in present_run_ids}
        rejected_unattributed = _zero_counts(present_run_ids)
        rejected_source_name = func.coalesce(run_sources_table.c.source_name, validation_errors_table.c.node_id).label("source_name")
        rejected_stmt = (
            select(
                validation_errors_table.c.run_id,
                rejected_source_name,
                func.count().label("count"),
            )
            .select_from(
                validation_errors_table.outerjoin(
                    run_sources_table,
                    and_(
                        run_sources_table.c.run_id == validation_errors_table.c.run_id,
                        run_sources_table.c.source_node_id == validation_errors_table.c.node_id,
                    ),
                )
            )
            .where(validation_errors_table.c.run_id.in_(present_run_ids))
            .where(validation_errors_table.c.destination == DISCARD_DESTINATION)
            .group_by(validation_errors_table.c.run_id, rejected_source_name)
        )
        for run_id, rejected_source, count in conn.execute(rejected_stmt):
            # validation_errors.node_id is nullable; an unattributable
            # rejection counts in the aggregate only (the per-source map
            # would otherwise invent a source name).
            if rejected_source is None:
                rejected_unattributed[str(run_id)] += int(count)
            else:
                rejected_rows_by_source[str(run_id)][str(rejected_source)] = int(count)

        emitted_stmt = (
            select(tokens_table.c.run_id, func.count().label("count"))
            .where(tokens_table.c.run_id.in_(present_run_ids))
            .group_by(tokens_table.c.run_id)
        )
        for run_id, count in conn.execute(emitted_stmt):
            emitted_tokens[str(run_id)] = int(count)

        terminal_stmt = (
            select(
                token_outcomes_table.c.run_id,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
                token_outcomes_table.c.sink_name,
                func.count().label("count"),
            )
            .where(token_outcomes_table.c.run_id.in_(present_run_ids))
            .where(token_outcomes_table.c.completed == 1)
            .group_by(
                token_outcomes_table.c.run_id,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
                token_outcomes_table.c.sink_name,
            )
        )
        for run_id_value, outcome, path, sink_name, count in conn.execute(terminal_stmt):
            run_id = str(run_id_value)
            value = int(count)
            terminal_tokens[run_id] += value
            if outcome == TerminalOutcome.SUCCESS.value:
                succeeded_tokens[run_id] += value
            elif outcome == TerminalOutcome.FAILURE.value:
                failed_tokens[run_id] += value
            elif outcome == TerminalOutcome.TRANSIENT.value:
                structural_tokens[run_id] += value

            if outcome == TerminalOutcome.SUCCESS.value and path == TerminalPath.GATE_ROUTED.value:
                routed_success[run_id] += value
            elif outcome == TerminalOutcome.FAILURE.value and path == TerminalPath.ON_ERROR_ROUTED.value:
                routed_failure[run_id] += value
            elif outcome == TerminalOutcome.FAILURE.value and path == TerminalPath.QUARANTINED_AT_SOURCE.value:
                quarantined[run_id] += value
            elif outcome == TerminalOutcome.FAILURE.value and (
                path in (TerminalPath.SINK_DISCARDED.value, TerminalPath.GATE_ERROR_DISCARDED.value) or sink_name == DISCARD_SINK_NAME
            ):
                discarded[run_id] += value

        # Per-token decision census. The outer join admits completed rows AND
        # ADR-038 (NULL, ABANDONED) rows; the conditional counts split them so
        # one pass classifies every emitted token as decided / abandoned /
        # unexplained.
        outcomes_by_emitted_token = (
            select(
                tokens_table.c.run_id.label("run_id"),
                tokens_table.c.token_id.label("token_id"),
                func.count(case((token_outcomes_table.c.completed == 1, 1))).label("completed_count"),
                func.count(case((token_outcomes_table.c.path == TerminalPath.ABANDONED.value, 1))).label("abandoned_count"),
            )
            .select_from(
                tokens_table.outerjoin(
                    token_outcomes_table,
                    and_(
                        token_outcomes_table.c.run_id == tokens_table.c.run_id,
                        token_outcomes_table.c.token_id == tokens_table.c.token_id,
                        or_(
                            token_outcomes_table.c.completed == 1,
                            token_outcomes_table.c.path == TerminalPath.ABANDONED.value,
                        ),
                    ),
                )
            )
            .where(tokens_table.c.run_id.in_(present_run_ids))
            .group_by(tokens_table.c.run_id, tokens_table.c.token_id)
            .subquery()
        )

        missing_stmt = (
            select(outcomes_by_emitted_token.c.run_id, func.count().label("count"))
            .where(outcomes_by_emitted_token.c.completed_count == 0)
            .where(outcomes_by_emitted_token.c.abandoned_count == 0)
            .group_by(outcomes_by_emitted_token.c.run_id)
        )
        for run_id, count in conn.execute(missing_stmt):
            missing_terminal_outcomes[str(run_id)] = int(count)

        abandoned_stmt = (
            select(outcomes_by_emitted_token.c.run_id, func.count().label("count"))
            .where(outcomes_by_emitted_token.c.completed_count == 0)
            .where(outcomes_by_emitted_token.c.abandoned_count > 0)
            .group_by(outcomes_by_emitted_token.c.run_id)
        )
        for run_id, count in conn.execute(abandoned_stmt):
            abandoned_tokens[str(run_id)] = int(count)

        duplicate_stmt = (
            select(outcomes_by_emitted_token.c.run_id, func.count().label("count"))
            .where(outcomes_by_emitted_token.c.completed_count > 1)
            .group_by(outcomes_by_emitted_token.c.run_id)
        )
        for run_id, count in conn.execute(duplicate_stmt):
            duplicate_terminal_outcomes[str(run_id)] = int(count)

        # ADR-038 contradiction guard: a token both decided AND abandoned
        # means the audit trail simultaneously claims a lifecycle answer
        # exists and that nothing would ever produce one.
        contradiction_stmt = (
            select(outcomes_by_emitted_token.c.run_id, func.count().label("count"))
            .where(outcomes_by_emitted_token.c.completed_count > 0)
            .where(outcomes_by_emitted_token.c.abandoned_count > 0)
            .group_by(outcomes_by_emitted_token.c.run_id)
        )
        for run_id, count in conn.execute(contradiction_stmt):
            violations_by_run[str(run_id)].append(
                f"{int(count)} token(s) both terminally decided and marked ABANDONED — audit contradiction (ADR-038)"
            )

    accounting: dict[str, RunAccounting] = {}
    corrupt: dict[str, RunAccountingCorruption] = {}
    for run_id in present_run_ids:
        violations = list(violations_by_run[run_id])
        if duplicate_terminal_outcomes[run_id] > 0:
            violations.append(f"{duplicate_terminal_outcomes[run_id]} token(s) with duplicate completed terminal outcomes")
        if violations:
            # Integrity failure is a per-run fact: the run ships an explicit
            # corruption marker INSTEAD of accounting, so no closure claim is
            # possible for it and healthy runs in the batch stay available.
            corrupt[run_id] = RunAccountingCorruption(
                landscape_run_id=run_id,
                violations=_bounded_violations(violations),
            )
            continue
        pending_tokens = missing_terminal_outcomes[run_id]
        source_rows = source_rows_by_source[run_id]
        source_row_total = sum(source_rows.values())
        rejected_rows = rejected_rows_by_source[run_id]
        rejected_row_total = sum(rejected_rows.values()) + rejected_unattributed[run_id]
        # Union of both maps: a source whose every row was rejected has no
        # rows_table entries at all, and dropping it from the per-source map
        # would erase the source from accounting (the g01 shape).
        per_source = {
            name: RunAccountingSource(
                rows_processed=source_rows.get(name, 0),
                rows_rejected=rejected_rows.get(name, 0),
                rows_read=source_rows.get(name, 0) + rejected_rows.get(name, 0),
            )
            for name in sorted(set(source_rows) | set(rejected_rows))
        }

        # ADR-038 closure taxonomy: 'closed' — every token decided;
        # 'abandoned' — none unexplained, but some explicitly abandoned;
        # 'open' — unexplained undecided tokens remain.
        closure: Literal["closed", "open", "abandoned", "unknown"]
        if (
            emitted_tokens[run_id] == terminal_tokens[run_id]
            and missing_terminal_outcomes[run_id] == 0
            and duplicate_terminal_outcomes[run_id] == 0
            and abandoned_tokens[run_id] == 0
        ):
            closure = "closed"
        elif missing_terminal_outcomes[run_id] == 0 and duplicate_terminal_outcomes[run_id] == 0 and abandoned_tokens[run_id] > 0:
            closure = "abandoned"
        else:
            closure = "open"
        try:
            accounting[run_id] = RunAccounting(
                source=RunAccountingSource(
                    rows_processed=source_row_total,
                    rows_rejected=rejected_row_total,
                    rows_read=source_row_total + rejected_row_total,
                ),
                sources=per_source,
                tokens=RunAccountingTokens(
                    emitted=emitted_tokens[run_id],
                    terminal=terminal_tokens[run_id],
                    succeeded=succeeded_tokens[run_id],
                    failed=failed_tokens[run_id],
                    structural=structural_tokens[run_id],
                    pending=pending_tokens,
                    abandoned=abandoned_tokens[run_id],
                ),
                routing=RunAccountingRouting(
                    routed_success=routed_success[run_id],
                    routed_failure=routed_failure[run_id],
                    quarantined=quarantined[run_id],
                    discarded=discarded[run_id],
                ),
                integrity=RunAccountingIntegrity(
                    closure=closure,
                    missing_terminal_outcomes=missing_terminal_outcomes[run_id],
                    duplicate_terminal_outcomes=duplicate_terminal_outcomes[run_id],
                ),
            )
        except ValidationError as exc:
            # Shape-valid counts can still contradict the response-model
            # invariants (token balance, routing subsets). That is the same
            # per-run integrity fact as a shape violation — isolate it rather
            # than letting one run's contradiction 500 the whole batch. The
            # pydantic messages carry only field names and counts.
            messages = [str(error["msg"]) for error in exc.errors(include_url=False, include_context=False, include_input=False)]
            corrupt[run_id] = RunAccountingCorruption(
                landscape_run_id=run_id,
                violations=_bounded_violations([f"accounting failed response validation: {message}" for message in messages]),
            )
    return RunAccountingBatch(accounting=accounting, corrupt=corrupt)


def load_run_accounting_from_db(db: LandscapeDB, *, landscape_run_id: str) -> RunAccounting:
    """Derive source/token/routing/integrity accounting for one Landscape run.

    Single-run reader for the run's OWN completion path (the execution
    service reads its just-finished run to settle status). Batch isolation is
    meaningless for a batch of one, and this caller must fail loudly, so a
    corrupt run raises with its violations.
    """
    batch = load_run_accounting_map_from_db(db, (landscape_run_id,))
    corruption = batch.corrupt.get(landscape_run_id)
    if corruption is not None:
        raise ValueError(
            f"Landscape accounting for run {landscape_run_id!r} failed integrity validation: {'; '.join(corruption.violations)}"
        )
    return batch.accounting[landscape_run_id]


def _zero_counts(run_ids: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(run_ids, 0)

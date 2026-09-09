"""Read virtual discard sink summaries from the Landscape audit database."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import and_, func, select
from sqlalchemy.engine.url import make_url

from elspeth.contracts import NodeStateStatus, NodeType
from elspeth.contracts.audit import DISCARD_SINK_NAME
from elspeth.contracts.enums import TerminalPath
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import (
    node_states_table,
    nodes_table,
    token_outcomes_table,
    transform_errors_table,
    validation_errors_table,
)
from elspeth.web.config import WebSettings
from elspeth.web.execution.schemas import DiscardStageSummary, DiscardSummary

DISCARD_DESTINATION = "discard"


def load_discard_summaries_for_settings(
    settings: WebSettings,
    landscape_run_ids: Iterable[str | None],
) -> dict[str, DiscardSummary]:
    """Load discard summaries for run IDs using web runtime settings.

    Missing SQLite audit files return an empty result so session-list tests
    and fresh local deployments do not create empty audit databases while
    rendering runs that predate Landscape execution. Existing but invalid
    audit databases still raise through ``LandscapeDB.from_url``.
    """
    run_ids = _unique_run_ids(landscape_run_ids)
    if not run_ids:
        return {}

    landscape_url = settings.get_landscape_url()
    if _sqlite_database_file_missing(landscape_url):
        return {}

    with LandscapeDB.from_url(
        landscape_url,
        passphrase=settings.landscape_passphrase,
        create_tables=False,
        read_only=True,
    ) as db:
        return load_discard_summaries_from_db(db, run_ids)


def load_discard_summaries_from_db(
    db: LandscapeDB,
    landscape_run_ids: Iterable[str],
) -> dict[str, DiscardSummary]:
    """Load discard summaries from an already-open Landscape database."""
    run_ids = _unique_run_ids(landscape_run_ids)
    if not run_ids:
        return {}

    counts: dict[str, dict[str, int]] = {
        run_id: {
            "validation_errors": 0,
            "transform_errors": 0,
            "gate_errors": 0,
            "sink_discards": 0,
        }
        for run_id in run_ids
    }
    stages: dict[str, list[DiscardStageSummary]] = {run_id: [] for run_id in run_ids}

    with db.read_only_connection() as conn:
        validation_query = (
            select(
                validation_errors_table.c.run_id,
                validation_errors_table.c.node_id,
                func.count().label("count"),
            )
            .where(validation_errors_table.c.run_id.in_(run_ids))
            .where(validation_errors_table.c.destination == DISCARD_DESTINATION)
            .group_by(validation_errors_table.c.run_id, validation_errors_table.c.node_id)
            .order_by(validation_errors_table.c.run_id.asc(), validation_errors_table.c.node_id.asc())
        )
        for run_id, node_id, count in conn.execute(validation_query):
            count_value = int(count)
            counts[run_id]["validation_errors"] += count_value
            stages[run_id].append(
                DiscardStageSummary(
                    stage="source_validation",
                    node_id=node_id,
                    count=count_value,
                )
            )

        transform_query = (
            select(
                transform_errors_table.c.run_id,
                transform_errors_table.c.transform_id,
                func.count().label("count"),
            )
            .where(transform_errors_table.c.run_id.in_(run_ids))
            .where(transform_errors_table.c.destination == DISCARD_DESTINATION)
            .group_by(transform_errors_table.c.run_id, transform_errors_table.c.transform_id)
            .order_by(transform_errors_table.c.run_id.asc(), transform_errors_table.c.transform_id.asc())
        )
        for run_id, transform_id, count in conn.execute(transform_query):
            count_value = int(count)
            counts[run_id]["transform_errors"] += count_value
            stages[run_id].append(
                DiscardStageSummary(
                    stage="transform_validation",
                    node_id=transform_id,
                    count=count_value,
                )
            )

        gate_error_query = (
            select(
                token_outcomes_table.c.run_id,
                node_states_table.c.node_id,
                func.count(func.distinct(token_outcomes_table.c.token_id)).label("count"),
            )
            .select_from(
                token_outcomes_table.join(
                    node_states_table,
                    and_(
                        token_outcomes_table.c.run_id == node_states_table.c.run_id,
                        token_outcomes_table.c.token_id == node_states_table.c.token_id,
                    ),
                ).join(
                    nodes_table,
                    and_(
                        node_states_table.c.run_id == nodes_table.c.run_id,
                        node_states_table.c.node_id == nodes_table.c.node_id,
                    ),
                )
            )
            .where(token_outcomes_table.c.run_id.in_(run_ids))
            .where(token_outcomes_table.c.path == TerminalPath.GATE_ERROR_DISCARDED)
            .where(token_outcomes_table.c.completed == 1)
            .where(node_states_table.c.status == NodeStateStatus.FAILED)
            .where(nodes_table.c.node_type == NodeType.GATE)
            .group_by(token_outcomes_table.c.run_id, node_states_table.c.node_id)
            .order_by(token_outcomes_table.c.run_id.asc(), node_states_table.c.node_id.asc())
        )
        for run_id, node_id, count in conn.execute(gate_error_query):
            count_value = int(count)
            counts[run_id]["gate_errors"] += count_value
            stages[run_id].append(
                DiscardStageSummary(
                    stage="gate_evaluation",
                    node_id=node_id,
                    count=count_value,
                )
            )

        # Attribute each sink discard to the sink that refused the row, as every
        # other stage above does (elspeth-9595abb7b0: the only stage entry named
        # no node, so the summary could not even say which sink discarded).
        #
        # A correlated scalar subquery, not a join: the discard sentinel lives on
        # token_outcomes while the node lives on the token's FAILED sink state,
        # and nothing in the schema constrains a token to one of those. Joining
        # would emit one row per state and inflate the stage total past the
        # category count DiscardSummary cross-checks, turning an attribution
        # question into a rejected response. Reducing to one node per token keeps
        # the total exactly what the unattributed query returned, whatever the
        # states say. To be clear about how much is claimed: this is defensive,
        # not observed — a discarded token is terminal, so no such shape has been
        # reproduced. The ordering picks the deepest state and is therefore a
        # best-effort ATTRIBUTION; only the COUNT is exact.
        #
        # LEFT-join semantics (a NULL node_id when no failed sink state exists)
        # are deliberate for the same reason: a discard whose primary anchor is
        # missing must still be counted, unattributed, rather than dropped.
        discard_node_id = (
            select(node_states_table.c.node_id)
            .select_from(
                node_states_table.join(
                    nodes_table,
                    and_(
                        node_states_table.c.run_id == nodes_table.c.run_id,
                        node_states_table.c.node_id == nodes_table.c.node_id,
                    ),
                )
            )
            .where(node_states_table.c.run_id == token_outcomes_table.c.run_id)
            .where(node_states_table.c.token_id == token_outcomes_table.c.token_id)
            .where(node_states_table.c.status == NodeStateStatus.FAILED)
            .where(nodes_table.c.node_type == NodeType.SINK)
            .order_by(node_states_table.c.step_index.desc(), node_states_table.c.node_id.asc())
            .limit(1)
            .scalar_subquery()
            .label("node_id")
        )
        attributed_discards = (
            select(token_outcomes_table.c.run_id.label("run_id"), discard_node_id)
            .where(token_outcomes_table.c.run_id.in_(run_ids))
            .where(token_outcomes_table.c.sink_name == DISCARD_SINK_NAME)
            .where(token_outcomes_table.c.completed == 1)
            .subquery()
        )
        sink_query = select(
            attributed_discards.c.run_id,
            attributed_discards.c.node_id,
            func.count().label("count"),
        ).group_by(attributed_discards.c.run_id, attributed_discards.c.node_id)
        sink_stages: dict[str, list[DiscardStageSummary]] = {run_id: [] for run_id in run_ids}
        for run_id, node_id, count in conn.execute(sink_query):
            count_value = int(count)
            counts[run_id]["sink_discards"] += count_value
            sink_stages[run_id].append(
                DiscardStageSummary(
                    stage="sink_discard",
                    node_id=node_id,
                    count=count_value,
                )
            )
        # Ordered here rather than in SQL: node_id is now nullable, and backends
        # disagree on where NULL sorts, so an ORDER BY would make the projection
        # differ between SQLite and PostgreSQL.
        for run_id, run_sink_stages in sink_stages.items():
            run_sink_stages.sort(key=lambda stage: (stage.node_id is None, stage.node_id or ""))
            stages[run_id].extend(run_sink_stages)

    summaries: dict[str, DiscardSummary] = {}
    for run_id, run_counts in counts.items():
        total = run_counts["validation_errors"] + run_counts["transform_errors"] + run_counts["gate_errors"] + run_counts["sink_discards"]
        if total > 0:
            summaries[run_id] = DiscardSummary(total=total, stages=tuple(stages[run_id]), **run_counts)
    return summaries


def _unique_run_ids(landscape_run_ids: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(sorted({run_id for run_id in landscape_run_ids if run_id}))


def _sqlite_database_file_missing(landscape_url: str) -> bool:
    parsed = make_url(landscape_url)
    if not parsed.drivername.startswith("sqlite"):
        return False
    database = parsed.database
    if database is None or database == ":memory:":
        return False
    # A SQLite ``file:`` URI (``?uri=true`` connections) carries the real path
    # in the URI's path component; treating the whole ``file:...`` string as a
    # literal filename would spuriously report an existing DB as missing.
    if database.startswith("file:"):
        from urllib.parse import urlparse

        database = urlparse(database).path
    return not Path(database).exists()

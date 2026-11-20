"""Production-schema fixtures for the ADR-019 I1a run-end orphan sweep."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Select, and_, select
from sqlalchemy.engine import Connection

from elspeth.contracts import NodeType
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import token_outcomes_table, token_parents_table, tokens_table
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db

# Outcome tuple: (token_id, run_id, outcome or None, path).
Outcome = tuple[str, str, str | None, str]
# Edge tuple: (child_token_id, parent_token_id, run_id).
Edge = tuple[str, str, str]

EXPECTED_ORPHANS: dict[str, list[tuple[str, str]]] = {
    "run-A": [
        ("a-fork-orphan", "fork_parent"),
        ("a-fork-orphan", "fork_parent"),
        ("a-expand-orphan", "expand_parent"),
        ("a-no-child-outcome", "fork_parent"),
        ("a-cross-child", "fork_parent"),
        ("a-cross-edge", "fork_parent"),
        ("a-chain-orphan", "fork_parent"),
        ("a-intermediate-orphan", "expand_parent"),
    ],
    "run-B": [("b-orphan", "expand_parent")],
}


def base_orphan_query(run_id: str) -> Select[tuple[str, str]]:
    """The single-statement I1a predicate the repository used at 72fad2c23 (the oracle)."""
    child_outcomes = token_outcomes_table.alias("child_outcomes")
    child_witness = (
        select(child_outcomes.c.outcome_id)
        .select_from(
            token_parents_table.join(
                child_outcomes,
                and_(child_outcomes.c.token_id == token_parents_table.c.token_id, child_outcomes.c.run_id == run_id),
            )
        )
        .where(token_parents_table.c.parent_token_id == token_outcomes_table.c.token_id)
        .where(token_parents_table.c.run_id == run_id)
    )
    return (
        select(token_outcomes_table.c.token_id, token_outcomes_table.c.path)
        .where(token_outcomes_table.c.run_id == run_id)
        .where(token_outcomes_table.c.path.in_(("fork_parent", "expand_parent")))
        .where(token_outcomes_table.c.outcome == "transient")
        .where(~child_witness.exists())
    )


def begin_test_run(factory: RecorderFactory, run_id: str) -> str:
    """Begin a run with one source row; return that row's id for token FKs."""
    factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id=run_id,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    source = factory.data_flow.register_node(
        plugin_name="test_source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        schema_config=SchemaConfig.from_dict({"mode": "observed"}),
        coordination_token=leader_coordination_token(factory, run_id),
    )
    row, _ = factory.data_flow.create_row_with_token(
        source.node_id,
        0,
        {"x": 1},
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=leader_coordination_token(factory, run_id),
    )
    return row.row_id


def make_database(url: str | None = None) -> tuple[LandscapeDB, RecorderFactory, dict[str, str]]:
    """Two begun runs through LandscapeDB schema creation and RecorderFactory wiring."""
    db = LandscapeDB.from_url(url) if url is not None else make_landscape_db()
    factory = make_factory(db)
    rows = {run_id: begin_test_run(factory, run_id) for run_id in ("run-A", "run-B")}
    return db, factory, rows


def insert_graph(conn: Connection, rows: dict[str, str], outcomes: list[Outcome], edges: list[Edge]) -> None:
    """Insert real token, outcome and parent rows with every FK target present.

    Tokens named only by ``outcomes``/``edges`` are created in the run that
    names them. The first non-null outcome of a token is its completed one.
    """
    token_runs = {token: run for token, run, _, _ in outcomes}
    for child, parent, run in edges:
        token_runs.setdefault(child, run)
        token_runs.setdefault(parent, run)
    insert_tokens(conn, rows, token_runs)
    insert_outcomes(conn, outcomes, completed_tokens=set())
    if edges:
        next_ordinal: dict[str, int] = {}
        edge_rows = []
        for child, parent, run in edges:
            ordinal = next_ordinal.get(child, 0)
            edge_rows.append({"token_id": child, "parent_token_id": parent, "run_id": run, "ordinal": ordinal})
            next_ordinal[child] = ordinal + 1
        conn.execute(token_parents_table.insert(), edge_rows)


def insert_tokens(conn: Connection, rows: dict[str, str], token_runs: dict[str, str]) -> None:
    """Insert one token per ``token_id -> run_id`` entry, on that run's source row."""
    if not token_runs:
        return
    now = datetime.now(UTC)
    conn.execute(
        tokens_table.insert(),
        [{"token_id": token, "run_id": run, "row_id": rows[run], "created_at": now} for token, run in token_runs.items()],
    )


def insert_outcomes(conn: Connection, outcomes: list[Outcome], *, completed_tokens: set[str]) -> None:
    """Insert outcome rows for existing tokens; ``completed_tokens`` already hold a terminal row."""
    if not outcomes:
        return
    now = datetime.now(UTC)
    outcome_rows = []
    for token, run, result, path in outcomes:
        completed = int(result is not None and token not in completed_tokens)
        if completed:
            completed_tokens.add(token)
        outcome_rows.append(
            {
                "outcome_id": uuid4().hex,
                "run_id": run,
                "token_id": token,
                "outcome": result,
                "path": path,
                "completed": completed,
                "recorded_at": now,
            }
        )
    conn.execute(token_outcomes_table.insert(), outcome_rows)


def populate_equivalence_cases(conn: Connection, rows: dict[str, str]) -> None:
    """Every witness/orphan case of the I1a predicate, across two runs, with a duplicate outer row."""
    outcomes: list[Outcome] = [
        ("a-fork-orphan", "run-A", "transient", "fork_parent"),
        ("a-fork-orphan", "run-A", "transient", "fork_parent"),
        ("a-expand-orphan", "run-A", "transient", "expand_parent"),
        ("a-witness", "run-A", "transient", "fork_parent"),
        ("a-no-child-outcome", "run-A", "transient", "fork_parent"),
        ("a-cross-child", "run-A", "transient", "fork_parent"),
        ("a-cross-edge", "run-A", "transient", "fork_parent"),
        ("a-buffered-child", "run-A", "transient", "expand_parent"),
        ("a-nontransient", "run-A", "success", "fork_parent"),
        ("a-nonparent", "run-A", "transient", "default_flow"),
        ("b-orphan", "run-B", "transient", "expand_parent"),
        ("b-witness", "run-B", "transient", "fork_parent"),
        ("child-good", "run-A", "success", "default_flow"),
        ("child-buffered", "run-A", None, "buffered"),
        ("child-cross", "run-B", "success", "default_flow"),
        ("child-cross-edge", "run-B", "success", "default_flow"),
        ("child-b-good", "run-B", "success", "default_flow"),
        ("a-multi-outcome", "run-A", "transient", "fork_parent"),
        ("child-multi-outcome", "run-A", None, "buffered"),
        ("child-multi-outcome", "run-A", "success", "default_flow"),
        ("a-multi-parent-1", "run-A", "transient", "fork_parent"),
        ("a-multi-parent-2", "run-A", "transient", "expand_parent"),
        ("child-multi-parent", "run-A", "success", "default_flow"),
        ("a-chain-orphan", "run-A", "transient", "fork_parent"),
        ("chain-grandchild", "run-A", "success", "default_flow"),
        ("a-chain-witness", "run-A", "transient", "fork_parent"),
        ("a-intermediate-orphan", "run-A", "transient", "expand_parent"),
        ("a-mixed", "run-A", "transient", "fork_parent"),
        ("mixed-good", "run-A", "success", "default_flow"),
        ("a-self-link", "run-A", "transient", "fork_parent"),
        ("a-cycle-one", "run-A", "transient", "fork_parent"),
        ("a-cycle-two", "run-A", "transient", "expand_parent"),
        ("a-null-outcome", "run-A", None, "fork_parent"),
    ]
    edges: list[Edge] = [
        ("child-good", "a-witness", "run-A"),
        ("child-no-outcome", "a-no-child-outcome", "run-A"),
        ("child-no-outcome-2", "a-no-child-outcome", "run-A"),
        ("child-a-cross", "a-cross-child", "run-A"),
        ("child-a-cross-edge", "a-cross-edge", "run-A"),
        ("child-buffered", "a-buffered-child", "run-A"),
        ("child-b-good", "b-witness", "run-B"),
        ("child-cross", "b-witness", "run-B"),
        ("child-cross-edge", "b-witness", "run-B"),
        ("child-multi-outcome", "a-multi-outcome", "run-A"),
        ("child-multi-parent", "a-multi-parent-1", "run-A"),
        ("child-multi-parent", "a-multi-parent-2", "run-A"),
        ("chain-intermediate", "a-chain-orphan", "run-A"),
        ("chain-grandchild", "chain-intermediate", "run-A"),
        ("a-intermediate-orphan", "a-chain-witness", "run-A"),
        ("intermediate-no-outcome", "a-intermediate-orphan", "run-A"),
        ("mixed-good", "a-mixed", "run-A"),
        ("mixed-no-outcome", "a-mixed", "run-A"),
        ("a-self-link", "a-self-link", "run-A"),
        ("a-cycle-one", "a-cycle-two", "run-A"),
        ("a-cycle-two", "a-cycle-one", "run-A"),
    ]
    insert_graph(conn, rows, outcomes, edges)


def populate_shape(db: LandscapeDB, rows: dict[str, str], shape: str, n: int) -> None:
    """Build an r8 benchmark shape in run-A through the production schema."""
    with db.engine.begin() as conn:
        if shape == "fork":
            outcomes: list[Outcome] = []
            edges: list[Edge] = []
            for index in range(n):
                parent, child = f"parent-{index:06d}", f"child-{index:06d}"
                outcomes.extend(((parent, "run-A", "transient", "fork_parent"), (child, "run-A", "success", "default_flow")))
                edges.append((child, parent, "run-A"))
            insert_graph(conn, rows, outcomes, edges)
        elif shape == "childless":
            insert_graph(conn, rows, [(f"p{i:06d}", "run-A", "transient", "fork_parent") for i in range(n)], [])
        elif shape == "star-plus-500":
            children = [f"c{i:06d}" for i in range(n)]
            star: list[Outcome] = [("p", "run-A", "transient", "fork_parent")]
            star.extend((f"orphan{i:04d}", "run-A", "transient", "fork_parent") for i in range(500))
            insert_graph(conn, rows, star, [(child, "p", "run-A") for child in children])
        elif shape == "chain":
            insert_graph(
                conn,
                rows,
                [(f"p{i:06d}", "run-A", "transient", "fork_parent") for i in range(n)],
                [(f"p{i + 1:06d}", f"p{i:06d}", "run-A") for i in range(n - 1)],
            )
        else:
            raise AssertionError(f"unknown shape {shape!r}")


def make_stale_statistics(db: LandscapeDB, rows: dict[str, str]) -> None:
    """Collect ANALYZE statistics while run-A holds one healthy fork.

    Rows appended afterwards are invisible to ``sqlite_stat1``: the naturally
    stale distribution that made the base predicate quadratic in r8.
    """
    with db.engine.begin() as conn:
        insert_graph(
            conn,
            rows,
            [("early-parent", "run-A", "transient", "fork_parent"), ("early-child", "run-A", "success", "default_flow")],
            [("early-child", "early-parent", "run-A")],
        )
    with db.engine.begin() as conn:
        conn.exec_driver_sql("ANALYZE")

"""The fenced source-quarantine ingest records a rejected row's whole audit record in ONE transaction.

QR-1 (DESIGN-QR, lane 5887): no sink-bound token exists only in memory. A row the
source rejects gets its ``rows`` + ``tokens`` record, the step-0 FAILED source
state, the DIVERT routing event on ``__quarantine__`` and a durable PENDING_SINK
handoff to its quarantine sink — together or not at all — so a resume re-drives
the parked item through the ordinary pending-sink drain and never re-derives the
rejected row through the source schema. Every case here runs against a real
Landscape through the production composition (``engine.tokens.ingest_source_quarantine``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select, update

from elspeth.contracts import NodeType, RoutingMode
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import NodeStateStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import RunLeadershipLostError
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import NodeID
from elspeth.core.canonical import sanitize_for_canonical
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    node_states_table,
    pending_sink_bundle_clause,
    routing_events_table,
    rows_table,
    scheduler_events_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.tokens import ingest_source_quarantine, quarantine_pipeline_row
from tests.fixtures.landscape import leader_token_for, make_factory, make_landscape_db

_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
_TABLES = (rows_table, tokens_table, node_states_table, routing_events_table, token_work_items_table, scheduler_events_table)
_ERROR = "id: [int_parsing]"


@dataclass(frozen=True)
class _Harness:
    db: LandscapeDB
    factory: RecorderFactory
    leader: CoordinationToken
    edge_id: str


@pytest.fixture
def harness() -> Iterator[_Harness]:
    db = make_landscape_db()
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    leader = leader_token_for(db, run.run_id)
    for node_id, node_type in (("source", NodeType.SOURCE), ("sink_bad", NodeType.SINK)):
        factory.data_flow.register_node(node_id, node_type, "1.0", {}, node_id=node_id, schema_config=_SCHEMA, coordination_token=leader)
    edge = factory.data_flow.register_edge("source", "sink_bad", "__quarantine__", RoutingMode.DIVERT, coordination_token=leader)
    try:
        yield _Harness(db, factory, leader, edge.edge_id)
    finally:
        db.close()


def _ingest(h: _Harness, row: object, *, leader: CoordinationToken | None = None, error: str = _ERROR):
    return ingest_source_quarantine(
        scheduler=h.factory.scheduler,
        data_flow=h.factory.data_flow,
        execution=h.factory.execution,
        coordination_token=leader or h.leader,
        source_node_id=NodeID("source"),
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        row=row,
        validation_error_id=None,
        quarantine_sink="bad",
        quarantine_error=error,
        quarantine_edge_id=h.edge_id,
        terminal_step_index=2,
    )


def _snapshot(h: _Harness) -> dict[str, tuple[tuple[object, ...], ...]]:
    with h.db.read_only_connection() as conn:
        return {table.name: tuple(tuple(row) for row in conn.execute(select(table))) for table in _TABLES}


def test_one_transaction_records_row_token_failed_state_divert_and_parked_handoff(harness: _Harness) -> None:
    result = _ingest(harness, {"id": "abc"})

    assert (result.outcome, result.path, result.sink_name) == (TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE, "bad")
    assert result.scheduler_pending_sink is True
    assert result.authoritative_error_hash == compute_error_hash(_ERROR)
    token_id = result.token.token_id
    with harness.db.read_only_connection() as conn:
        (state,) = conn.execute(select(node_states_table).where(node_states_table.c.token_id == token_id)).mappings().all()
        (route,) = conn.execute(select(routing_events_table).where(routing_events_table.c.state_id == state["state_id"])).mappings().all()
        (item,) = conn.execute(select(token_work_items_table).where(token_work_items_table.c.token_id == token_id)).mappings().all()
        (event,) = conn.execute(select(scheduler_events_table).where(scheduler_events_table.c.token_id == token_id)).mappings().all()
        bundle_complete = conn.execute(
            select(pending_sink_bundle_clause()).where(token_work_items_table.c.work_item_id == item["work_item_id"])
        ).scalar_one()
        outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.token_id == token_id)).all()
    assert (state["node_id"], state["step_index"], state["status"]) == ("source", 0, NodeStateStatus.FAILED.value)
    assert json.loads(state["error_json"]) == {"exception": _ERROR, "type": "ValidationError"}
    assert (route["edge_id"], route["mode"]) == (harness.edge_id, RoutingMode.DIVERT.value)
    # Born parked on the node_id-NULL terminal lane, owner-attributed to the leader.
    assert item["status"] == TokenWorkStatus.PENDING_SINK.value
    assert item["node_id"] is None
    assert (item["pending_sink_name"], item["pending_outcome"], item["pending_path"]) == (
        "bad",
        TerminalOutcome.FAILURE.value,
        TerminalPath.QUARANTINED_AT_SOURCE.value,
    )
    assert (item["pending_error_hash"], item["pending_error_message"]) == (compute_error_hash(_ERROR), _ERROR)
    assert (item["lease_owner"], item["lease_expires_at"]) == (harness.leader.worker_id, None)
    assert bundle_complete
    assert (event["event_type"], event["from_status"], event["to_status"]) == (
        SchedulerEventType.MARK_PENDING_SINK.value,
        None,
        TokenWorkStatus.PENDING_SINK.value,
    )
    # The outcome is recorded after sink durability, never at ingest.
    assert outcomes == []


@pytest.mark.parametrize(
    "row",
    [sanitize_for_canonical({"x": float("nan")}), ["not", "an", "object"], 42],
    ids=["sanitised_non_finite_value", "list_row", "scalar_row"],
)
def test_parked_payload_round_trips_to_the_exact_live_audit_row(harness: _Harness, row: object) -> None:
    """The resumed sink write rebuilds the SAME member row the live write would (effect reuse, M1).

    The router sanitises non-finite values before the ingest, so the parked
    payload is the sanitised row (NaN -> None), exactly as the live write sees it.
    """
    result = _ingest(harness, row)

    with harness.db.read_only_connection() as conn:
        payload = conn.execute(
            select(token_work_items_table.c.row_payload_json).where(token_work_items_table.c.token_id == result.token.token_id)
        ).scalar_one()
    restored = harness.factory.scheduler.deserialize_row_payload(payload)
    live = quarantine_pipeline_row(row)
    assert restored.to_dict() == live.to_dict() == result.final_data.to_dict()
    assert restored.contract == live.contract


def test_non_canonical_rejected_data_is_recorded_through_the_quarantined_hash_fallback(harness: _Harness) -> None:
    """Kill mutant: ``quarantined=True`` -> ``False`` on the row insert.

    An integer outside the canonical-JSON domain survives sanitisation (a JSON
    source parses it) and fails canonical hashing; only the quarantined
    ``repr_hash`` fallback records it.
    """
    result = _ingest(harness, {"bad_data": 2**64})

    row = harness.factory.query.get_row(result.token.row_id)
    assert row is not None
    assert row.source_data_hash is not None


def test_stale_leader_epoch_refuses_the_whole_ingest_with_no_mutation(harness: _Harness) -> None:
    before = _snapshot(harness)
    with pytest.raises(RunLeadershipLostError):
        _ingest(harness, {"id": "abc"}, leader=replace(harness.leader, leader_epoch=harness.leader.leader_epoch + 1))
    assert _snapshot(harness) == before


@pytest.mark.parametrize(
    "statement_prefix",
    ["INSERT INTO NODE_STATES", "INSERT INTO ROUTING_EVENTS", "INSERT INTO TOKEN_WORK_ITEMS", "INSERT INTO SCHEDULER_EVENTS"],
)
def test_a_fault_anywhere_inside_the_ingest_leaves_nothing(harness: _Harness, statement_prefix: str) -> None:
    """W0a: no token without its FAILED state, routing event and handoff — one transaction (mutation M6)."""
    before = _snapshot(harness)

    def fail(_conn: object, _cursor: object, statement: str, _parameters: object, _context: object, _executemany: bool) -> None:
        if statement.lstrip().upper().startswith(statement_prefix):
            raise RuntimeError("injected quarantine-ingest fault")

    sqlalchemy_event.listen(harness.db.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected quarantine-ingest fault"):
            _ingest(harness, {"id": "abc"})
    finally:
        sqlalchemy_event.remove(harness.db.engine, "before_cursor_execute", fail)
    assert _snapshot(harness) == before


def test_a_resuming_leader_claims_the_parked_quarantine_handoff(harness: _Harness) -> None:
    result = _ingest(harness, {"id": "abc"})

    claimed = harness.factory.scheduler.claim_pending_sink(
        coordination_token=harness.leader, lease_owner=harness.leader.worker_id, lease_seconds=60
    )

    assert claimed is not None
    assert claimed.token_id == result.token.token_id
    assert (claimed.pending_path, claimed.pending_error_hash) == (TerminalPath.QUARANTINED_AT_SOURCE.value, compute_error_hash(_ERROR))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("pending_error_hash", None),
        ("pending_error_message", None),
        ("join_group_id", "join-1"),
    ],
)
def test_bundle_predicate_refuses_an_incomplete_source_quarantine_bundle(harness: _Harness, column: str, value: object) -> None:
    """C1: the fifth arm admits the source-quarantine pair ONLY with its complete evidence."""
    result = _ingest(harness, {"id": "abc"})
    with harness.db.engine.begin() as conn:
        conn.execute(
            update(token_work_items_table).where(token_work_items_table.c.token_id == result.token.token_id).values({column: value})
        )
        complete = conn.execute(
            select(pending_sink_bundle_clause()).where(token_work_items_table.c.token_id == result.token.token_id)
        ).scalar_one()
    assert not complete

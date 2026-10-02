"""Bulk audit inserts count rows returned by the database, including trigger omissions."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Table, select

from elspeth.contracts import NodeType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import BarrierTerminalOutcomeSpec
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape.data_flow.outcomes import TokenOutcomeWrite, record_terminal_outcomes_guarded
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import (
    coalesce_effect_members_table,
    metadata,
    token_lineage_frames_table,
    token_outcomes_table,
    token_parents_table,
    tokens_table,
)
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db, register_test_node
from tests.helpers.postgres_target import postgres_test_target

_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)
_TARGETS = {
    "child_tokens": tokens_table,
    "child_parents": token_parents_table,
    "lineage_frames": token_lineage_frames_table,
    "coalesce_parents": token_parents_table,
    "coalesce_members": coalesce_effect_members_table,
    "general_outcomes": token_outcomes_table,
    "terminal_outcomes": token_outcomes_table,
}


@pytest.fixture(scope="module", params=["sqlite", pytest.param("postgres", marks=pytest.mark.testcontainer)])
def database(request: pytest.FixtureRequest) -> Iterator[LandscapeDB]:
    if request.param == "sqlite":
        db = make_landscape_db()
        try:
            yield db
        finally:
            db.close()
    else:
        with postgres_test_target(driver="psycopg") as url:
            db = LandscapeDB.from_url(url)
            try:
                yield db
            finally:
                db.close()


def _snapshot(db: LandscapeDB) -> dict[str, tuple[tuple[object, ...], ...]]:
    with db.read_only_connection() as conn:
        return {
            table.name: tuple(tuple(row) for row in conn.execute(select(table).order_by(*table.primary_key.columns)))
            for table in sorted(metadata.tables.values(), key=lambda table: table.name)
        }


@contextmanager
def _omit_second_insert(db: LandscapeDB, table: Table, run_id: str) -> Iterator[None]:
    """A real BEFORE trigger silently omits the second row of this run's batch."""
    with db.engine.begin() as conn:
        baseline = len(conn.execute(select(table).where(table.c.run_id == run_id)).all())
        # Names are fixed owned schema names; run IDs come from begin_run's UUID mint.
        condition = f"NEW.run_id = '{run_id}' AND (SELECT COUNT(*) FROM {table.name} WHERE run_id = '{run_id}') = {baseline + 1}"
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql(
                f"CREATE TRIGGER omit_second_audit_insert BEFORE INSERT ON {table.name} WHEN {condition} BEGIN SELECT RAISE(IGNORE); END"
            )
        else:
            conn.exec_driver_sql(
                "CREATE FUNCTION omit_second_audit_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF {condition} THEN RETURN NULL; END IF; RETURN NEW; END $$"
            )
            conn.exec_driver_sql(
                f"CREATE TRIGGER omit_second_audit_insert BEFORE INSERT ON {table.name} "
                "FOR EACH ROW EXECUTE FUNCTION omit_second_audit_insert()"
            )
    try:
        yield
    finally:
        with db.engine.begin() as conn:
            if conn.dialect.name == "sqlite":
                conn.exec_driver_sql("DROP TRIGGER omit_second_audit_insert")
            else:
                conn.exec_driver_sql(f"DROP TRIGGER omit_second_audit_insert ON {table.name}")
                conn.exec_driver_sql("DROP FUNCTION omit_second_audit_insert()")


def _prepare_write(db: LandscapeDB, seam: str) -> tuple[str, Callable[[], None]]:
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    leader = leader_coordination_token(factory, run.run_id)
    member = leader.membership
    source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE)
    transform_id = register_test_node(factory.data_flow, run.run_id, "transform")
    _, token = factory.data_flow.create_row_with_token(
        source_id, 0, {"value": 1}, coordination_token=leader, source_row_index=0, ingest_sequence=0
    )
    ref = TokenRef(token.token_id, run.run_id)

    if seam in ("child_tokens", "child_parents", "lineage_frames"):

        def expand() -> None:
            factory.data_flow.expand_token(ref, token.row_id, [{"value": 2}, {"value": 3}], output_contract=_CONTRACT, member_token=member)

        return run.run_id, expand

    if seam in ("coalesce_parents", "coalesce_members"):
        claim = factory.scheduler.enqueue_ready_claimed(
            member_token=member,
            token_id=token.token_id,
            row_id=token.row_id,
            node_id=transform_id,
            step_index=1,
            ingest_sequence=0,
            row_payload_json="{}",
            lease_owner=member.worker_id,
            lease_seconds=300,
        )
        children, _ = factory.data_flow.fork_token(ref, token.row_id, ["left", "right"], member_token=member, work_item=claim)
        refs = [TokenRef(child.token_id, run.run_id) for child in children]

        def coalesce() -> None:
            factory.data_flow.coalesce_tokens(refs, token.row_id, {"value": 4}, merged_contract=_CONTRACT, coordination_token=leader)

        return run.run_id, coalesce

    other = factory.data_flow.create_token(token.row_id, coordination_token=leader)
    refs = [ref, TokenRef(other.token_id, run.run_id)]

    def outcomes() -> None:
        with fenced_leader_transaction(db.engine, token=leader, window_seconds=300, verb="bulk_cardinality_control") as conn:
            if seam == "general_outcomes":
                factory.data_flow.outcomes.record_token_outcomes_on(
                    conn,
                    run_id=run.run_id,
                    outcomes=[TokenOutcomeWrite(item, TerminalOutcome.SUCCESS, TerminalPath.COALESCED) for item in refs],
                )
            else:
                record_terminal_outcomes_guarded(
                    conn,
                    run_id=run.run_id,
                    outcomes=[BarrierTerminalOutcomeSpec(item.token_id, TerminalOutcome.SUCCESS, TerminalPath.COALESCED) for item in refs],
                    recorded_at=token.created_at,
                )

    return run.run_id, outcomes


@pytest.mark.parametrize("seam", tuple(_TARGETS))
@pytest.mark.parametrize("omit_second", [False, True], ids=["complete", "trigger_omission"])
def test_bulk_insert_requires_every_actual_row(database: LandscapeDB, seam: str, omit_second: bool) -> None:
    run_id, write = _prepare_write(database, seam)
    table = _TARGETS[seam]
    before = _snapshot(database)
    if omit_second:
        with (
            _omit_second_insert(database, table, run_id),
            pytest.raises((AuditIntegrityError, LandscapeRecordError), match="incomplete batch"),
        ):
            write()
        # Includes tokens, lineage, parent links, effects, outcomes, and fence stamps.
        assert _snapshot(database) == before
    else:
        write()
        after = _snapshot(database)
        assert len(after[table.name]) == len(before[table.name]) + 2

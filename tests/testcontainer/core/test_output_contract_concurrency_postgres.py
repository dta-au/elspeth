"""Real PostgreSQL contention proof for node output-contract evolution."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event, select
from tests.fixtures.landscape import leader_coordination_token, make_factory
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import NodeType
from elspeth.contracts.coordination import WorkerMembershipToken
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.schema import nodes_table

pytestmark = pytest.mark.testcontainer

_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


def _contract(name: str, python_type: type) -> SchemaContract:
    return SchemaContract(
        mode="OBSERVED",
        fields=(
            FieldContract(
                normalized_name=name,
                original_name=name,
                python_type=python_type,
                required=True,
                source="inferred",
            ),
        ),
        locked=True,
    )


@pytest.mark.timeout(120)
def test_concurrent_postgres_writers_lock_and_merge_disjoint_contracts(postgres_url: str) -> None:
    first_db = LandscapeDB.from_url(postgres_url)
    second_db = LandscapeDB.from_url(postgres_url)
    winner_holds_node = threading.Event()
    loser_reached_node = threading.Event()
    release_winner = threading.Event()
    backends: dict[str, int] = {}
    threads: tuple[threading.Thread, ...] = ()

    def before_execute(conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        normalized = " ".join(statement.upper().split())
        if "FROM NODES" in normalized and "FOR UPDATE" in normalized:
            name = threading.current_thread().name
            backends[name] = int(conn.connection.driver_connection.info.backend_pid)
            if name == "contract-second":
                loser_reached_node.set()

    def after_execute(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        normalized = " ".join(statement.upper().split())
        if "FROM NODES" in normalized and "FOR UPDATE" in normalized and threading.current_thread().name == "contract-first":
            winner_holds_node.set()
            if not release_winner.wait(timeout=30):
                raise TimeoutError("test did not release output-contract node lock")

    for db in (first_db, second_db):
        event.listen(db.engine, "before_cursor_execute", before_execute)
        event.listen(db.engine, "after_cursor_execute", after_execute)
    try:
        first = make_factory(first_db)
        second = make_factory(second_db)
        first.run_lifecycle.begin_run(
            config={},
            canonical_version="v1",
            run_id="run-1",
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
        first.data_flow.register_node(
            coordination_token=leader_coordination_token(first, "run-1"),
            plugin_name="mapper",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0.0",
            config={},
            node_id="xfm",
            output_contract=_contract("base", int),
            schema_config=_SCHEMA,
        )
        first_member = leader_coordination_token(first, "run-1").membership
        second_member = RunCoordinationRepository(second_db.engine).admit_follower(
            run_id="run-1", worker_id="contract-peer", config_hash=stable_hash({}), window_seconds=60
        )
        failures: list[BaseException] = []

        def update(factory: RecorderFactory, contract: SchemaContract, member_token: WorkerMembershipToken) -> None:
            try:
                factory.data_flow.update_node_output_contract("xfm", contract, member_token=member_token)
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = (
            threading.Thread(target=update, name="contract-first", args=(first, _contract("left", str), first_member)),
            threading.Thread(target=update, name="contract-second", args=(second, _contract("right", float), second_member)),
        )
        threads[0].start()
        assert winner_holds_node.wait(timeout=10)
        threads[1].start()
        assert loser_reached_node.wait(timeout=10)
        winner_pid = backends["contract-first"]
        loser_pid = backends["contract-second"]
        assert winner_pid != loser_pid
        with first_db.read_only_connection() as conn:
            for _ in range(200):
                blocked = conn.exec_driver_sql("SELECT %s = ANY(pg_blocking_pids(%s))", (winner_pid, loser_pid)).scalar_one()
                if blocked:
                    break
                threading.Event().wait(0.01)
        assert blocked, "PostgreSQL did not report the contender waiting on the output-contract node"
        release_winner.set()
        for thread in threads:
            thread.join(timeout=30)

        assert not any(thread.is_alive() for thread in threads)
        assert failures == []
        _, stored = first.data_flow.get_node_contracts("run-1", "xfm")
        assert stored is not None
        assert {field.normalized_name for field in stored.fields} == {"base", "left", "right"}
        with first_db.read_only_connection() as conn:
            stored_hash = conn.execute(
                select(nodes_table.c.output_contract_hash).where((nodes_table.c.run_id == "run-1") & (nodes_table.c.node_id == "xfm"))
            ).scalar_one()
        assert stored_hash == stored.version_hash()
    finally:
        release_winner.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=30)
        for db in (first_db, second_db):
            event.remove(db.engine, "before_cursor_execute", before_execute)
            event.remove(db.engine, "after_cursor_execute", after_execute)
        second_db.close()
        first_db.close()

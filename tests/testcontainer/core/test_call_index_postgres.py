"""PostgreSQL proof for database-owned call-index collision recovery."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Literal

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from tests.fixtures.landscape import leader_coordination_token
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_coordination_table, run_workers_table

pytestmark = pytest.mark.testcontainer

_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


def _seed_parent(
    factory: RecorderFactory, parent_kind: Literal["state", "operation"]
) -> tuple[str, CoordinationToken, TokenWorkItem | None]:
    run_id = f"call-race-{parent_kind}"
    factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=run_id, leader_worker_id=f"call-writer-{parent_kind}")
    authority = leader_coordination_token(factory, run_id)
    factory.data_flow.register_node(
        coordination_token=authority,
        plugin_name="source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        node_id=f"source-{parent_kind}",
        schema_config=_SCHEMA,
    )
    if parent_kind == "operation":
        operation = factory.execution.begin_operation(f"source-{parent_kind}", "source_load", coordination_token=authority)
        return operation.operation_id, authority, None

    factory.data_flow.register_node(
        coordination_token=authority,
        plugin_name="transform",
        node_type=NodeType.TRANSFORM,
        plugin_version="1.0",
        config={},
        node_id="transform-state",
        schema_config=_SCHEMA,
    )
    row, token = factory.data_flow.create_row_with_token(
        "source-state",
        0,
        {"value": 1},
        coordination_token=authority,
        source_row_index=0,
        ingest_sequence=0,
    )
    item = factory.scheduler.enqueue_ready_claimed(
        member_token=authority.membership,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id="transform-state",
        step_index=0,
        ingest_sequence=0,
        row_payload_json='{"value":1}',
        lease_owner=authority.worker_id,
        lease_seconds=60,
    )
    state = factory.execution.begin_node_state(
        token.token_id,
        "transform-state",
        0,
        {"value": 1},
        member_token=authority.membership,
    )
    return state.state_id, authority, item


def _physical_connection(conn: Connection) -> tuple[int, int]:
    driver_connection = conn.connection.driver_connection
    return id(driver_connection), int(driver_connection.info.backend_pid)


@pytest.mark.parametrize("parent_kind", ["state", "operation"])
@pytest.mark.timeout(120)
def test_postgres_completed_effects_survive_call_index_collision(
    postgres_url: str,
    parent_kind: Literal["state", "operation"],
) -> None:
    db = LandscapeDB.from_url(postgres_url)
    first = RecorderFactory(db)
    second = RecorderFactory(db)
    parent_id, authority, work_item = _seed_parent(first, parent_kind)
    if parent_kind == "state":
        assert work_item is not None
        proposed = [
            factory.execution.allocate_call_index(parent_id, member_token=authority.membership, work_item=work_item)
            for factory in (first, second)
        ]
    else:
        proposed = [factory.execution.allocate_operation_call_index(parent_id, coordination_token=authority) for factory in (first, second)]
    assert proposed == [0, 0]

    connected = {"call-first": threading.Event(), "call-second": threading.Event()}
    physical: dict[str, tuple[int, int]] = {}
    effects: list[str] = []
    outcomes: dict[str, tuple[str, int | str]] = {}
    lock = threading.Lock()

    def record_writer_connection(conn: Connection) -> None:
        thread_name = threading.current_thread().name
        if thread_name in connected:
            with lock:
                physical.setdefault(thread_name, _physical_connection(conn))
            connected[thread_name].set()

    event.listen(db.engine, "begin", record_writer_connection)

    def worker(name: str, factory: RecorderFactory) -> None:
        # The observable effect is complete before the contended audit insert.
        with lock:
            effects.append(name)
        try:
            if parent_kind == "state":
                assert work_item is not None
                call = factory.execution.record_call(
                    parent_id,
                    0,
                    CallType.HTTP,
                    CallStatus.SUCCESS,
                    request_data=RawCallPayload({"worker": name}),
                    response_data=RawCallPayload({"ok": True}),
                    member_token=authority.membership,
                    work_item=work_item,
                )
            else:
                call = factory.execution.record_operation_call(
                    parent_id,
                    CallType.HTTP,
                    CallStatus.SUCCESS,
                    request_data=RawCallPayload({"worker": name}),
                    response_data=RawCallPayload({"ok": True}),
                    call_index=0,
                    coordination_token=authority,
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            result: tuple[str, int | str] = (type(exc).__name__, str(exc))
        else:
            result = ("ok", call.call_index)
        with lock:
            outcomes[name] = result

    threads = [
        threading.Thread(target=worker, name="call-first", args=("first", first)),
        threading.Thread(target=worker, name="call-second", args=("second", second)),
    ]
    try:
        # Hold the authority row before either writer enters its parent fence.
        # An INSERT barrier would deadlock: the second writer cannot reach the
        # payload INSERT while the first writer holds that fence. PostgreSQL's
        # wait graph supplies the interleaving witness before we release it.
        with db.engine.begin() as holder:
            holder_pid = _physical_connection(holder)[1]
            table = run_workers_table if parent_kind == "state" else run_coordination_table
            parent_lock = select(table.c.run_id).where(table.c.run_id == authority.run_id)
            if parent_kind == "state":
                parent_lock = parent_lock.where(table.c.worker_id == authority.worker_id)
            assert holder.execute(parent_lock.with_for_update()).scalar_one() == authority.run_id
            prior_waiter: int | None = None
            with db.engine.connect() as observer:
                for thread in threads:
                    thread.start()
                    assert connected[thread.name].wait(timeout=10), outcomes
                    with lock:
                        writer_pid = physical[thread.name][1]
                    deadline = time.monotonic() + 10
                    while True:
                        blockers = observer.execute(select(func.pg_blocking_pids(writer_pid))).scalar_one()
                        if holder_pid in blockers or (prior_waiter is not None and prior_waiter in blockers):
                            break
                        with lock:
                            assert not outcomes, outcomes
                        assert time.monotonic() < deadline, f"PostgreSQL did not report {thread.name} waiting at its fence"
                        time.sleep(0.01)
                    prior_waiter = writer_pid
            with lock:
                assert sorted(effects) == ["first", "second"]
                assert outcomes == {}
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()

        assert sorted(effects) == ["first", "second"]
        assert sorted(outcomes.values(), key=lambda item: str(item[1])) == [("ok", 0), ("ok", 1)]
        assert set(physical) == {"call-first", "call-second"}
        first_connection, second_connection = physical.values()
        assert first_connection[0] != second_connection[0]
        assert first_connection[1] != second_connection[1]
        calls = first.query.get_calls(parent_id) if parent_kind == "state" else first.execution.get_operation_calls(parent_id)
        assert [call.call_index for call in calls] == [0, 1]
    finally:
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=30)
        event.remove(db.engine, "begin", record_writer_connection)
        db.close()

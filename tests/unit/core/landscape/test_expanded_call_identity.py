"""Call parents bind to durable expansion positions, including corrupt evidence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event, Lock, local

import pytest
from sqlalchemy import delete, event, update

import elspeth.core.landscape.execution.calls as calls_module
from elspeth.contracts import CallStatus, CallType, RunStatus
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.schema import calls_table, runs_table, token_parents_table
from tests.fixtures.landscape import claim_test_work_item, leader_coordination_token, register_test_node
from tests.unit.core.landscape.test_call_mode_persistence import _two_runs


@pytest.mark.parametrize(
    "corruption",
    [
        None,
        "duplicate_member",
        "duplicate_member_distinct_request",
        "duplicate_member_distinct_type",
        "duplicate_member_no_calls",
        "missing_parent",
        "cycle",
        "cross_row",
    ],
)
@pytest.mark.parametrize("kind", ["expand", "fork"])
def test_expanded_call_identity_is_order_independent_and_rejects_corruption(corruption: str | None, kind: str) -> None:
    factory, _source_operation, _current_operation = _two_runs()
    children_by_run = {}
    states_by_run = {}
    contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
    for run_id in ("source", "current"):
        authority = leader_coordination_token(factory, run_id)
        register_test_node(factory.data_flow, run_id, "transform", plugin_name="transform")
        row, token = factory.data_flow.create_row_with_token(
            "source-node", 0, {"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=authority
        )
        if kind == "expand":
            children, _group = factory.data_flow.expand_token(
                TokenRef(token_id=token.token_id, run_id=run_id),
                row.row_id,
                [{"value": 1}, {"value": 1}],
                output_contract=contract,
                member_token=authority.membership,
            )
        else:
            children, _group = factory.data_flow.fork_token(
                TokenRef(token_id=token.token_id, run_id=run_id),
                row.row_id,
                ["left", "right"],
                member_token=authority.membership,
                work_item=claim_test_work_item(factory, member_token=authority.membership, token_id=token.token_id, node_id=None),
            )
        children_by_run[run_id] = children
        states_by_run[run_id] = [
            factory.execution.begin_node_state(child.token_id, "transform", 2, {"value": 1}, member_token=authority.membership)
            for child in children
        ]
    # Insert in reverse member order; neither timestamps nor call IDs encode
    # the correct mapping. Each parent-local occurrence is index zero.
    with factory._db.write_connection() as conn:
        for index in (1, 0):
            conn.execute(
                calls_table.insert().values(
                    call_id=f"recorded-{1 - index}",
                    state_id=states_by_run["source"][index].state_id,
                    operation_id=None,
                    call_index=0,
                    call_type=CallType.HTTP.value,
                    status=CallStatus.SUCCESS.value,
                    request_hash=stable_hash({"url": "https://example.test/a"}),
                    response_hash=stable_hash({"member": index}),
                    created_at=datetime.now(UTC),
                )
            )
        second = children_by_run["source"][1]
        if corruption in (
            "duplicate_member",
            "duplicate_member_distinct_request",
            "duplicate_member_distinct_type",
            "duplicate_member_no_calls",
        ):
            conn.execute(update(token_parents_table).where(token_parents_table.c.token_id == second.token_id).values(ordinal=0))
            if corruption == "duplicate_member_distinct_request":
                conn.execute(
                    update(calls_table)
                    .where(calls_table.c.call_id == "recorded-0")
                    .values(request_hash=stable_hash({"url": "https://example.test/b"}))
                )
            elif corruption == "duplicate_member_distinct_type":
                conn.execute(update(calls_table).where(calls_table.c.call_id == "recorded-0").values(call_type=CallType.LLM.value))
            elif corruption == "duplicate_member_no_calls":
                conn.execute(delete(calls_table).where(calls_table.c.call_id == "recorded-0"))
        elif corruption == "missing_parent":
            conn.execute(delete(token_parents_table).where(token_parents_table.c.token_id == second.token_id))
        elif corruption == "cycle":
            conn.execute(
                update(token_parents_table).where(token_parents_table.c.token_id == second.token_id).values(parent_token_id=second.token_id)
            )
    if corruption == "cross_row":
        authority = leader_coordination_token(factory, "source")
        _other_row, other = factory.data_flow.create_row_with_token(
            "source-node", 1, {"value": 1}, source_row_index=1, ingest_sequence=1, coordination_token=authority
        )
        with factory._db.write_connection() as conn:
            conn.execute(
                update(token_parents_table).where(token_parents_table.c.token_id == second.token_id).values(parent_token_id=other.token_id)
            )
    with factory._db.write_connection() as conn:
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == "source")
            .values(status=RunStatus.COMPLETED.value, completed_at=datetime.now(UTC))
        )

    def lookup(index: int):
        return factory.execution.find_call_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            request_hash=stable_hash({"url": "https://example.test/a"}),
            current_state_id=states_by_run["current"][index].state_id,
            current_operation_id=None,
            current_call_index=0,
        )

    if corruption is not None:
        with pytest.raises(AuditIntegrityError, match=r"ambiguous|lineage"):
            lookup(0)
    else:
        assert lookup(1).call_id == "recorded-0"
        assert lookup(0).call_id == "recorded-1"


def test_expanded_call_lookup_reads_source_siblings_once_per_parent_group() -> None:
    factory, _source_operation, _current_operation = _two_runs()
    states_by_run = {}
    contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
    member_count = 16
    for run_id in ("source", "current"):
        authority = leader_coordination_token(factory, run_id)
        register_test_node(factory.data_flow, run_id, "transform", plugin_name="transform")
        row, token = factory.data_flow.create_row_with_token(
            "source-node", 0, {"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=authority
        )
        children, _group = factory.data_flow.expand_token(
            TokenRef(token_id=token.token_id, run_id=run_id),
            row.row_id,
            [{"value": 1}] * member_count,
            output_contract=contract,
            member_token=authority.membership,
        )
        states_by_run[run_id] = [
            factory.execution.begin_node_state(child.token_id, "transform", 2, {"value": 1}, member_token=authority.membership)
            for child in children
        ]
    with factory._db.write_connection() as conn:
        for index, state in enumerate(states_by_run["source"]):
            conn.execute(
                calls_table.insert().values(
                    call_id=f"recorded-{index}",
                    state_id=state.state_id,
                    operation_id=None,
                    call_index=0,
                    call_type=CallType.HTTP.value,
                    status=CallStatus.SUCCESS.value,
                    request_hash=stable_hash({"url": "https://example.test/a"}),
                    created_at=datetime.now(UTC),
                )
            )
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == "source")
            .values(status=RunStatus.COMPLETED.value, completed_at=datetime.now(UTC))
        )

    selects = []

    def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(factory._db.engine, "before_cursor_execute", count_selects)
    try:
        for index, state in enumerate(states_by_run["current"]):
            found = factory.execution.find_call_for_current_parent(
                source_run_id="source",
                call_type=CallType.HTTP,
                request_hash=stable_hash({"url": "https://example.test/a"}),
                current_state_id=state.state_id,
                current_operation_id=None,
                current_call_index=0,
            )
            assert found is not None and found.call_id == f"recorded-{index}"
    finally:
        event.remove(factory._db.engine, "before_cursor_execute", count_selects)

    # The first lookup validates all source siblings. Later lookups still
    # validate their current token, but do not rescan the completed source run.
    assert selects
    assert len(selects) <= member_count * 12


def test_unmatched_current_lineage_has_no_source_calls() -> None:
    factory, _source_operation, _current_operation = _two_runs()
    contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
    current_state_id = "current-unmatched-state"
    for run_id in ("source", "current"):
        authority = leader_coordination_token(factory, run_id)
        register_test_node(factory.data_flow, run_id, "transform", plugin_name="transform")
        row, token = factory.data_flow.create_row_with_token(
            "source-node", 0, {"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=authority
        )
        if run_id == "current":
            children, _group = factory.data_flow.expand_token(
                TokenRef(token_id=token.token_id, run_id=run_id),
                row.row_id,
                [{"value": 1}],
                output_contract=contract,
                member_token=authority.membership,
            )
            token = children[0]
        factory.execution.begin_node_state(
            token.token_id,
            "transform",
            0,
            {"value": 1},
            state_id=f"{run_id}-unmatched-state",
            member_token=authority.membership,
        )
    with factory._db.write_connection() as conn:
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == "source")
            .values(status=RunStatus.COMPLETED.value, completed_at=datetime.now(UTC))
        )
    assert (
        factory.execution.list_source_calls_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            current_state_id=current_state_id,
            current_operation_id=None,
        )
        == []
    )
    assert (
        factory.execution.find_call_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            request_hash=None,
            current_state_id=current_state_id,
            current_operation_id=None,
            current_call_index=0,
        )
        is None
    )


def test_source_parent_index_eviction_reloads_completed_source_row() -> None:
    factory, _source_operation, _current_operation = _two_runs()
    states_by_row = []
    for run_id in ("source", "current"):
        register_test_node(factory.data_flow, run_id, "transform", plugin_name="transform")
    for row_index in range(17):
        for run_id in ("source", "current"):
            authority = leader_coordination_token(factory, run_id)
            _row, token = factory.data_flow.create_row_with_token(
                "source-node",
                row_index,
                {"value": row_index},
                source_row_index=row_index,
                ingest_sequence=row_index,
                coordination_token=authority,
            )
            state = factory.execution.begin_node_state(
                token.token_id, "transform", 0, {"value": row_index}, member_token=authority.membership
            )
            if run_id == "current":
                states_by_row.append(state.state_id)
    with factory._db.write_connection() as conn:
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == "source")
            .values(status=RunStatus.COMPLETED.value, completed_at=datetime.now(UTC))
        )

    parent_queries = []

    def count_parent_queries(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "node_states.state_id, node_states.token_id, tokens.row_id" in statement:
            parent_queries.append(statement)

    event.listen(factory._db.engine, "before_cursor_execute", count_parent_queries)
    try:

        def lookup(row_index: int) -> None:
            assert (
                factory.execution.list_source_calls_for_current_parent(
                    source_run_id="source",
                    call_type=CallType.HTTP,
                    current_state_id=states_by_row[row_index],
                    current_operation_id=None,
                )
                == []
            )

        lookup(0)
        assert len(parent_queries) == 1
        lookup(0)
        assert len(parent_queries) == 1
        for row_index in range(1, 17):
            lookup(row_index)
        assert len(parent_queries) == 17
        lookup(0)
        assert len(parent_queries) == 18
    finally:
        event.remove(factory._db.engine, "before_cursor_execute", count_parent_queries)


@pytest.mark.parametrize("fail_first", [False, True])
def test_completed_source_parent_index_build_is_single_flight_and_retryable(fail_first: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _source_operation, _current_operation = _two_runs()
    with factory._db.write_connection() as conn:
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == "source")
            .values(status=RunStatus.COMPLETED.value, completed_at=datetime.now(UTC))
        )
    repository = factory.execution.calls
    second_miss_finished = Event()
    first_query_started = Event()

    class NotifyingLock:
        def __init__(self) -> None:
            self.lock = Lock()
            self.local = local()
            self.entries = 0

        def __enter__(self):
            self.lock.acquire()
            self.entries += 1
            self.local.entry = self.entries
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback):
            entry = self.local.entry
            self.lock.release()
            if entry == 2:
                second_miss_finished.set()

    monkeypatch.setattr(repository, "_source_parent_index_lock", NotifyingLock())
    original_fetchall = repository._ops.execute_fetchall
    parent_queries = 0
    count_lock = Lock()

    def counted_fetchall(query):
        nonlocal parent_queries
        if "node_states.state_id, node_states.token_id, tokens.row_id" in str(query):
            with count_lock:
                parent_queries += 1
                query_number = parent_queries
            if query_number == 1:
                first_query_started.set()
                assert second_miss_finished.wait(timeout=5), "second lookup did not reach the cache miss"
                if fail_first:
                    raise RuntimeError("injected source index read failure")
        return original_fetchall(query)

    monkeypatch.setattr(repository._ops, "execute_fetchall", counted_fetchall)
    kwargs = {
        "source_run_id": "source",
        "node_id": "transform",
        "step_index": 0,
        "attempt": 0,
        "source_node_id": "source-node",
        "source_row_index": 0,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(repository._source_parent_index, **kwargs)
        assert first_query_started.wait(timeout=5), "first lookup did not start its source scan"
        second = pool.submit(repository._source_parent_index, **kwargs)
        if fail_first:
            for lookup in (first, second):
                with pytest.raises(RuntimeError, match="injected source index read failure"):
                    lookup.result(timeout=5)
            assert parent_queries == 1
            assert repository._source_parent_index(**kwargs) == {}
            assert parent_queries == 2
            assert not repository._source_parent_builds
        else:
            assert first.result(timeout=5) == {}
            assert second.result(timeout=5) == {}
            assert parent_queries == 1
            assert not repository._source_parent_builds


def test_completed_source_parent_index_builds_distinct_rows_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _source_operation, _current_operation = _two_runs()
    with factory._db.write_connection() as conn:
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == "source")
            .values(status=RunStatus.COMPLETED.value, completed_at=datetime.now(UTC))
        )
    repository = factory.execution.calls
    original_fetchall = repository._ops.execute_fetchall
    both_queries = Barrier(2)

    def synchronized_fetchall(query):
        if "node_states.state_id, node_states.token_id, tokens.row_id" in str(query):
            both_queries.wait(timeout=5)
        return original_fetchall(query)

    monkeypatch.setattr(repository._ops, "execute_fetchall", synchronized_fetchall)
    kwargs = {
        "source_run_id": "source",
        "node_id": "transform",
        "step_index": 0,
        "attempt": 0,
        "source_node_id": "source-node",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(repository._source_parent_index, **kwargs, source_row_index=0)
        second = pool.submit(repository._source_parent_index, **kwargs, source_row_index=1)
        assert first.result(timeout=6) == {}
        assert second.result(timeout=6) == {}
        assert not repository._source_parent_builds


def test_completed_source_parent_waiter_survives_lru_eviction_during_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _source_operation, _current_operation = _two_runs()
    with factory._db.write_connection() as conn:
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == "source")
            .values(status=RunStatus.COMPLETED.value, completed_at=datetime.now(UTC))
        )
    repository = factory.execution.calls
    publish_started = Event()
    release_publish = Event()
    second_attached = Event()
    caller = local()
    first_future = True
    original_future = calls_module.Future

    class PausingFuture(original_future):
        def __init__(self) -> None:
            nonlocal first_future
            super().__init__()
            self.pause = first_future
            first_future = False

        def set_result(self, result) -> None:
            if self.pause:
                publish_started.set()
                assert release_publish.wait(timeout=10), "first builder was not released"
            super().set_result(result)

        def result(self, timeout=None):
            if self.pause and caller.is_second:
                second_attached.set()
            return super().result(timeout)

    monkeypatch.setattr(calls_module, "Future", PausingFuture)
    parent_queries = []

    def count_parent_queries(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "node_states.state_id, node_states.token_id, tokens.row_id" in statement:
            parent_queries.append(statement)

    event.listen(factory._db.engine, "before_cursor_execute", count_parent_queries)
    kwargs = {
        "source_run_id": "source",
        "node_id": "transform",
        "step_index": 0,
        "attempt": 0,
        "source_node_id": "source-node",
    }

    def second_lookup():
        caller.is_second = True
        return repository._source_parent_index(**kwargs, source_row_index=0)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(repository._source_parent_index, **kwargs, source_row_index=0)
            assert publish_started.wait(timeout=5), "first build did not reach result publication"
            for row_index in range(1, 17):
                assert repository._source_parent_index(**kwargs, source_row_index=row_index) == {}
            assert len(parent_queries) == 17
            second = pool.submit(second_lookup)
            try:
                assert second_attached.wait(timeout=5), "same-key waiter rebuilt after LRU eviction"
                assert len(parent_queries) == 17
            finally:
                release_publish.set()
            assert first.result(timeout=5) == {}
            assert second.result(timeout=5) == {}
            assert not repository._source_parent_builds
    finally:
        release_publish.set()
        event.remove(factory._db.engine, "before_cursor_execute", count_parent_queries)

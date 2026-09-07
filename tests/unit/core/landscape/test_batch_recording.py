from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from elspeth.contracts import BatchStatus, NodeType, TriggerType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape import LandscapeDB, run_coordination_repository
from elspeth.core.landscape.execution.batches import add_batch_member_guarded
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import batch_members_table, batches_table, rows_table, tokens_table
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db, make_recorder_with_run, register_test_node

_DYNAMIC_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})


def _batch_token(factory: RecorderFactory, batch_id: str) -> CoordinationToken:
    batch = factory.execution.get_batch(batch_id)
    return leader_coordination_token(factory, "run-1" if batch is None else batch.run_id)


def _add_batch_member(
    factory: RecorderFactory,
    batch_id: str,
    token_id: str,
    ordinal: int,
    *,
    conn: Connection | None = None,
) -> Any:
    if conn is not None:
        run_id = conn.execute(select(batches_table.c.run_id).where(batches_table.c.batch_id == batch_id)).scalar_one()
        return add_batch_member_guarded(conn, batch_id=batch_id, token_id=token_id, ordinal=ordinal, expected_run_id=run_id)
    token = _batch_token(factory, batch_id)
    with fenced_leader_transaction(factory.execution._db.engine, token=token, window_seconds=300, verb="test_batch_member") as active_conn:
        return add_batch_member_guarded(active_conn, batch_id=batch_id, token_id=token_id, ordinal=ordinal, expected_run_id=token.run_id)


def _register_artifact(factory: RecorderFactory, run_id: str, *args: Any, conn: Connection | None = None, **kwargs: Any) -> Any:
    if conn is not None:
        return factory.execution.artifacts.register_artifact(run_id, *args, **kwargs, conn=conn)
    token = leader_coordination_token(factory, run_id)
    with fenced_leader_transaction(factory.execution._db.engine, token=token, window_seconds=300, verb="test_artifact") as conn:
        return factory.execution.artifacts.register_artifact(run_id, *args, **kwargs, conn=conn)


def _work_item_for_token(factory: RecorderFactory, token_id: str) -> TokenWorkItem:
    with factory.execution._db.read_only_connection() as conn:
        row = conn.execute(
            select(rows_table)
            .join(tokens_table, rows_table.c.row_id == tokens_table.c.row_id)
            .where(
                tokens_table.c.token_id == token_id,
            )
        ).one()
    token = leader_coordination_token(factory, row.run_id)
    return factory.scheduler.enqueue_ready_claimed(
        member_token=token.membership,
        token_id=token_id,
        row_id=row.row_id,
        node_id=None,
        step_index=0,
        ingest_sequence=row.ingest_sequence,
        row_payload_json="{}",
        lease_owner=token.worker_id,
        lease_seconds=300,
    )


def _setup(*, run_id: str = "run-1") -> tuple[LandscapeDB, RecorderFactory]:
    setup = make_recorder_with_run(run_id=run_id, source_node_id="source-0", source_plugin_name="csv")
    register_test_node(setup.data_flow, setup.run_id, "agg-1", node_type=NodeType.AGGREGATION, plugin_name="aggregator")
    return setup.db, setup.factory


def _setup_with_token(
    *,
    run_id: str = "run-1",
) -> tuple[LandscapeDB, RecorderFactory]:
    db, factory = _setup(run_id=run_id)
    factory.data_flow.create_row_with_token(
        "source-0",
        0,
        {"name": "test"},
        row_id="row-1",
        source_row_index=0,
        ingest_sequence=0,
        token_id="tok-1",
        coordination_token=leader_coordination_token(factory, run_id),
    )
    factory.data_flow.create_row_with_token(
        "source-0",
        1,
        {"name": "test2"},
        row_id="row-2",
        source_row_index=1,
        ingest_sequence=1,
        token_id="tok-2",
        coordination_token=leader_coordination_token(factory, run_id),
    )
    return db, factory


def _setup_with_sink(
    *,
    run_id: str = "run-1",
) -> tuple[LandscapeDB, RecorderFactory]:
    """Setup with token and a sink node for artifact tests."""
    db, factory = _setup_with_token(run_id=run_id)
    factory.data_flow.register_node(
        plugin_name="csv_sink",
        node_type=NodeType.SINK,
        plugin_version="1.0",
        config={},
        node_id="sink-0",
        schema_config=_DYNAMIC_SCHEMA,
        coordination_token=leader_coordination_token(factory, run_id),
    )
    return db, factory


def _setup_two_runs_with_batch_integrity_records() -> tuple[LandscapeDB, RecorderFactory]:
    """Create two runs with tokens and node states for cross-run ownership tests."""
    db = make_landscape_db()
    factory = make_factory(db)

    for run_id, suffix in (("run-A", "A"), ("run-B", "B")):
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=run_id)
        factory.data_flow.register_node(
            plugin_name="csv",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            node_id="source-0",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, run_id),
        )
        factory.data_flow.register_node(
            plugin_name="aggregator",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id="agg-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, run_id),
        )
        factory.data_flow.register_node(
            plugin_name="csv_sink",
            node_type=NodeType.SINK,
            plugin_version="1.0",
            config={},
            node_id="sink-0",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, run_id),
        )
        factory.data_flow.create_row_with_token(
            "source-0",
            0,
            {"run": run_id},
            row_id=f"row-{suffix}",
            source_row_index=0,
            ingest_sequence=0,
            token_id=f"tok-{suffix}",
            coordination_token=leader_coordination_token(factory, run_id),
        )
        factory.execution.begin_node_state(
            f"tok-{suffix}",
            "agg-1",
            0,
            {"run": run_id},
            state_id=f"state-{suffix}",
            member_token=leader_coordination_token(factory, run_id).membership,
        )

    return db, factory


# ---------------------------------------------------------------------------
# create_batch
# ---------------------------------------------------------------------------


class TestCreateBatch:
    """Tests for BatchRecordingMixin.create_batch."""

    def test_creates_batch_with_draft_status(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.status == BatchStatus.DRAFT

    def test_generates_batch_id_when_not_provided(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.batch_id is not None
        assert isinstance(batch.batch_id, str)
        assert len(batch.batch_id) > 0

    def test_uses_provided_batch_id(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch(
            "agg-1", batch_id="my-batch-42", coordination_token=leader_coordination_token(factory, "run-1")
        )

        assert batch.batch_id == "my-batch-42"

    def test_stores_run_id_and_node_id(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.run_id == "run-1"
        assert batch.aggregation_node_id == "agg-1"

    def test_default_attempt_is_zero(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.attempt == 0

    def test_explicit_attempt_number(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", attempt=3, coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.attempt == 3

    def test_created_at_is_set(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.created_at is not None

    def test_trigger_fields_initially_none(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.trigger_type is None
        assert batch.trigger_reason is None

    def test_completed_at_initially_none(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.completed_at is None

    def test_aggregation_state_id_initially_none(self):
        _db, factory = _setup()
        batch = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch.aggregation_state_id is None

    def test_multiple_batches_get_unique_ids(self):
        _db, factory = _setup()
        batch_a = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))
        batch_b = factory.execution.create_batch("agg-1", coordination_token=leader_coordination_token(factory, "run-1"))

        assert batch_a.batch_id != batch_b.batch_id


# ---------------------------------------------------------------------------
# add_batch_member / get_batch_members
# ---------------------------------------------------------------------------


class TestAddBatchMember:
    """Tests for BatchRecordingMixin.add_batch_member."""

    def test_adds_member_to_batch(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        member = _add_batch_member(factory, "b-1", "tok-1", ordinal=0)

        assert member.batch_id == "b-1"
        assert member.token_id == "tok-1"
        assert member.ordinal == 0

    def test_roundtrip_via_get_batch_members(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-1", "tok-1", ordinal=0)
        _add_batch_member(factory, "b-1", "tok-2", ordinal=1)

        members = factory.execution.get_batch_members("b-1")

        assert len(members) == 2
        assert members[0].token_id == "tok-1"
        assert members[0].ordinal == 0
        assert members[1].token_id == "tok-2"
        assert members[1].ordinal == 1

    def test_multiple_members_different_ordinals(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-1", "tok-1", ordinal=5)
        _add_batch_member(factory, "b-1", "tok-2", ordinal=2)

        members = factory.execution.get_batch_members("b-1")

        assert len(members) == 2
        # Should be ordered by ordinal
        assert members[0].ordinal == 2
        assert members[1].ordinal == 5

    def test_rejects_token_from_different_run(self):
        """Batch membership must reject tokens owned by another run."""
        _db, factory = _setup_two_runs_with_batch_integrity_records()
        factory.execution.create_batch("agg-1", batch_id="batch-A", coordination_token=leader_coordination_token(factory, "run-A"))

        with pytest.raises(AuditIntegrityError, match=r"belongs to run 'run-B'.*belongs to run 'run-A'"):
            _add_batch_member(factory, "batch-A", "tok-B", ordinal=0)

        assert factory.execution.get_batch_members("batch-A") == []

    @pytest.mark.parametrize(
        "status",
        [BatchStatus.EXECUTING, BatchStatus.COMPLETED, BatchStatus.FAILED],
    )
    def test_rejects_every_non_draft_batch_status(self, status: BatchStatus) -> None:
        """DRAFT is the sole status in which batch membership is mutable."""
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        if status is BatchStatus.EXECUTING:
            factory.execution.update_batch_status("b-1", status, coordination_token=_batch_token(factory, "b-1"))
        else:
            factory.execution.complete_batch("b-1", status, coordination_token=_batch_token(factory, "b-1"))

        with pytest.raises(AuditIntegrityError, match=rf"status {status.value!r}.*immutable"):
            _add_batch_member(factory, "b-1", "tok-1", ordinal=0)

        assert factory.execution.get_batch_members("b-1") == []

    def test_nonexistent_batch_fails_without_membership(self) -> None:
        _db, factory = _setup_with_token()

        with pytest.raises(AuditIntegrityError, match="batch missing not found"):
            _add_batch_member(factory, "missing", "tok-1", ordinal=0)

        assert factory.execution.get_batch_members("missing") == []

    def test_caller_connection_preserves_draft_guard_and_atomic_write(self) -> None:
        db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        with db.write_connection() as conn:
            member = _add_batch_member(factory, "b-1", "tok-1", ordinal=0, conn=conn)

        assert member.run_id == "run-1"
        assert [record.token_id for record in factory.execution.get_batch_members("b-1")] == ["tok-1"]

    def test_caller_connection_rejects_closed_batch_without_poisoning_transaction(self) -> None:
        db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="closed", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.update_batch_status("closed", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "closed"))
        factory.execution.create_batch("agg-1", batch_id="open", coordination_token=leader_coordination_token(factory, "run-1"))

        with db.write_connection() as conn:
            with pytest.raises(AuditIntegrityError, match=r"status 'executing'.*immutable"):
                _add_batch_member(factory, "closed", "tok-1", ordinal=0, conn=conn)
            _add_batch_member(factory, "open", "tok-1", ordinal=0, conn=conn)

        assert factory.execution.get_batch_members("closed") == []
        assert [member.token_id for member in factory.execution.get_batch_members("open")] == ["tok-1"]

    def test_duplicate_membership_remains_a_conflict(self) -> None:
        """The hardening does not turn natural-key collisions into success."""
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-1", "tok-1", ordinal=0)

        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            _add_batch_member(factory, "b-1", "tok-1", ordinal=0)

        assert len(factory.execution.get_batch_members("b-1")) == 1

    def test_duplicate_ordinal_remains_a_conflict(self) -> None:
        """Two different tokens cannot occupy the same batch ordinal."""
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-1", "tok-1", ordinal=0)

        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            _add_batch_member(factory, "b-1", "tok-2", ordinal=0)

        assert [member.token_id for member in factory.execution.get_batch_members("b-1")] == ["tok-1"]

    def test_failure_after_insert_rolls_back_membership(self) -> None:
        """An exception after the INSERT cannot leave a partially committed member."""
        db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        def fail_after_member_insert(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
            if statement.lstrip().upper().startswith("INSERT INTO BATCH_MEMBERS"):
                raise RuntimeError("injected post-insert failure")

        event.listen(db.engine, "after_cursor_execute", fail_after_member_insert)
        try:
            with pytest.raises(RuntimeError, match="injected post-insert failure"):
                _add_batch_member(factory, "b-1", "tok-1", ordinal=0)
        finally:
            event.remove(db.engine, "after_cursor_execute", fail_after_member_insert)

        with db.read_only_connection() as conn:
            assert conn.execute(select(func.count()).select_from(batch_members_table)).scalar_one() == 0


# ---------------------------------------------------------------------------
# update_batch_status
# ---------------------------------------------------------------------------


class TestUpdateBatchStatus:
    """Tests for BatchRecordingMixin.update_batch_status."""

    def test_updates_status_to_executing(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        factory.execution.update_batch_status("b-1", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "b-1"))

        updated = factory.execution.get_batch("b-1")
        assert updated.status == BatchStatus.EXECUTING

    def test_does_not_set_completed_at_for_executing(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        factory.execution.update_batch_status("b-1", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "b-1"))

        updated = factory.execution.get_batch("b-1")
        assert updated.completed_at is None

    def test_rejects_terminal_target_statuses(self):
        """Terminal transitions must go through complete_batch()."""
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        for terminal_status in (BatchStatus.COMPLETED, BatchStatus.FAILED):
            with pytest.raises(AuditIntegrityError, match="complete_batch"):
                factory.execution.update_batch_status("b-1", terminal_status, coordination_token=_batch_token(factory, "b-1"))

        updated = factory.execution.get_batch("b-1")
        assert updated.status == BatchStatus.DRAFT
        assert updated.completed_at is None

    def test_sets_trigger_type(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        factory.execution.update_batch_status(
            "b-1", BatchStatus.EXECUTING, trigger_type=TriggerType.COUNT, coordination_token=_batch_token(factory, "b-1")
        )

        updated = factory.execution.get_batch("b-1")
        assert updated.trigger_type == TriggerType.COUNT

    def test_sets_trigger_reason(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        factory.execution.update_batch_status(
            "b-1",
            BatchStatus.EXECUTING,
            trigger_type=TriggerType.TIMEOUT,
            trigger_reason="30s elapsed",
            coordination_token=_batch_token(factory, "b-1"),
        )

        updated = factory.execution.get_batch("b-1")
        assert updated.trigger_type == TriggerType.TIMEOUT
        assert updated.trigger_reason == "30s elapsed"

    def test_sets_state_id(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.begin_node_state(
            "tok-1", "agg-1", 0, {"data": "test"}, state_id="state-1", member_token=leader_coordination_token(factory, "run-1").membership
        )

        factory.execution.update_batch_status(
            "b-1", BatchStatus.EXECUTING, state_id="state-1", coordination_token=_batch_token(factory, "b-1")
        )

        updated = factory.execution.get_batch("b-1")
        assert updated.aggregation_state_id == "state-1"

    def test_rejects_state_from_different_run(self):
        """Batch flush state must belong to the same run as the batch."""
        _db, factory = _setup_two_runs_with_batch_integrity_records()
        factory.execution.create_batch("agg-1", batch_id="batch-A", coordination_token=leader_coordination_token(factory, "run-A"))

        with pytest.raises(AuditIntegrityError):
            factory.execution.update_batch_status(
                "batch-A", BatchStatus.EXECUTING, state_id="state-B", coordination_token=_batch_token(factory, "batch-A")
            )

    def test_rejects_transition_from_completed(self):
        """Terminal status COMPLETED cannot transition to any other status (M2)."""
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.complete_batch("b-1", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-1"))

        with pytest.raises(AuditIntegrityError, match="terminal status"):
            factory.execution.update_batch_status("b-1", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "b-1"))

    def test_rejects_transition_from_failed(self):
        """Terminal status FAILED cannot transition to any other status (M2)."""
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.complete_batch("b-1", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-1"))

        with pytest.raises(AuditIntegrityError, match="terminal status"):
            factory.execution.update_batch_status("b-1", BatchStatus.DRAFT, coordination_token=_batch_token(factory, "b-1"))

    def test_rejects_nonexistent_batch(self):
        """Updating status of nonexistent batch raises AuditIntegrityError (M2)."""
        _db, factory = _setup()

        with pytest.raises(AuditIntegrityError, match="not found"):
            factory.execution.update_batch_status(
                "nonexistent", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "nonexistent")
            )


# ---------------------------------------------------------------------------
# complete_batch
# ---------------------------------------------------------------------------


class TestCompleteBatch:
    """Tests for BatchRecordingMixin.complete_batch."""

    def test_returns_updated_batch(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        result = factory.execution.complete_batch("b-1", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-1"))

        assert result.batch_id == "b-1"
        assert result.status == BatchStatus.COMPLETED

    def test_completed_at_is_populated(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        result = factory.execution.complete_batch("b-1", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-1"))

        assert result.completed_at is not None

    def test_complete_with_trigger_info(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        result = factory.execution.complete_batch(
            "b-1",
            BatchStatus.COMPLETED,
            trigger_type=TriggerType.COUNT,
            trigger_reason="reached 10 rows",
            coordination_token=_batch_token(factory, "b-1"),
        )

        assert result.trigger_type == TriggerType.COUNT
        assert result.trigger_reason == "reached 10 rows"

    def test_complete_as_failed(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        result = factory.execution.complete_batch("b-1", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-1"))

        assert result.status == BatchStatus.FAILED
        assert result.completed_at is not None

    def test_complete_with_state_id(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.begin_node_state(
            "tok-1", "agg-1", 0, {"data": "test"}, state_id="state-1", member_token=leader_coordination_token(factory, "run-1").membership
        )

        result = factory.execution.complete_batch(
            "b-1", BatchStatus.COMPLETED, state_id="state-1", coordination_token=_batch_token(factory, "b-1")
        )

        assert result.aggregation_state_id == "state-1"

    def test_complete_batch_persists_to_database(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.complete_batch(
            "b-1",
            BatchStatus.COMPLETED,
            trigger_type=TriggerType.END_OF_SOURCE,
            trigger_reason="source exhausted",
            coordination_token=_batch_token(factory, "b-1"),
        )

        fetched = factory.execution.get_batch("b-1")
        assert fetched.status == BatchStatus.COMPLETED
        assert fetched.trigger_type == TriggerType.END_OF_SOURCE
        assert fetched.trigger_reason == "source exhausted"

    def test_rejects_non_terminal_status_executing(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        with pytest.raises(AuditIntegrityError, match="terminal status"):
            factory.execution.complete_batch("b-1", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "b-1"))

    def test_rejects_non_terminal_status_draft(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        with pytest.raises(AuditIntegrityError, match="terminal status"):
            factory.execution.complete_batch("b-1", BatchStatus.DRAFT, coordination_token=_batch_token(factory, "b-1"))

    def test_accepts_completed_status(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        result = factory.execution.complete_batch("b-1", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-1"))
        assert result.status == BatchStatus.COMPLETED
        assert result.completed_at is not None


# ---------------------------------------------------------------------------
# get_batch
# ---------------------------------------------------------------------------


class TestGetBatch:
    """Tests for BatchRecordingMixin.get_batch."""

    def test_roundtrip(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        fetched = factory.execution.get_batch("b-1")

        assert fetched is not None
        assert fetched.batch_id == "b-1"
        assert fetched.run_id == "run-1"
        assert fetched.aggregation_node_id == "agg-1"
        assert fetched.status == BatchStatus.DRAFT

    def test_returns_none_for_unknown_id(self):
        _db, factory = _setup()

        result = factory.execution.get_batch("nonexistent-batch")

        assert result is None

    def test_reflects_status_updates(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.update_batch_status("b-1", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "b-1"))

        fetched = factory.execution.get_batch("b-1")

        assert fetched.status == BatchStatus.EXECUTING


# ---------------------------------------------------------------------------
# get_batches
# ---------------------------------------------------------------------------


class TestGetBatches:
    """Tests for BatchRecordingMixin.get_batches."""

    def test_lists_all_batches_for_run(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-2", coordination_token=leader_coordination_token(factory, "run-1"))

        batches = factory.execution.get_batches("run-1")

        assert len(batches) == 2
        batch_ids = {b.batch_id for b in batches}
        assert batch_ids == {"b-1", "b-2"}

    def test_empty_for_unknown_run(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))

        batches = factory.execution.get_batches("run-unknown")

        assert batches == []

    def test_filter_by_status(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-2", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.complete_batch("b-2", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-2"))

        draft_batches = factory.execution.get_batches("run-1", status=BatchStatus.DRAFT)
        completed_batches = factory.execution.get_batches(
            "run-1",
            status=BatchStatus.COMPLETED,
        )

        assert len(draft_batches) == 1
        assert draft_batches[0].batch_id == "b-1"
        assert len(completed_batches) == 1
        assert completed_batches[0].batch_id == "b-2"

    def test_filter_by_node_id(self):
        _db, factory = _setup()
        factory.data_flow.register_node(
            plugin_name="aggregator2",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id="agg-2",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-2", batch_id="b-2", coordination_token=leader_coordination_token(factory, "run-1"))

        agg1_batches = factory.execution.get_batches("run-1", node_id="agg-1")
        agg2_batches = factory.execution.get_batches("run-1", node_id="agg-2")

        assert len(agg1_batches) == 1
        assert agg1_batches[0].batch_id == "b-1"
        assert len(agg2_batches) == 1
        assert agg2_batches[0].batch_id == "b-2"

    def test_filter_by_status_and_node_id(self):
        _db, factory = _setup()
        factory.data_flow.register_node(
            plugin_name="aggregator2",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id="agg-2",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-2", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-2", batch_id="b-3", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.complete_batch("b-2", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-2"))

        result = factory.execution.get_batches(
            "run-1",
            status=BatchStatus.DRAFT,
            node_id="agg-1",
        )

        assert len(result) == 1
        assert result[0].batch_id == "b-1"

    def test_does_not_return_batches_from_other_runs(self):
        db = make_landscape_db()
        factory = make_factory(db)
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="run-1")
        factory.data_flow.register_node(
            plugin_name="aggregator",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id="agg-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="run-2")
        factory.data_flow.register_node(
            plugin_name="aggregator",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id="agg-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-2"),
        )
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-2", coordination_token=leader_coordination_token(factory, "run-2"))

        run1_batches = factory.execution.get_batches("run-1")
        run2_batches = factory.execution.get_batches("run-2")

        assert len(run1_batches) == 1
        assert run1_batches[0].batch_id == "b-1"
        assert len(run2_batches) == 1
        assert run2_batches[0].batch_id == "b-2"


# ---------------------------------------------------------------------------
# get_incomplete_batches
# ---------------------------------------------------------------------------


class TestGetIncompleteBatches:
    """Tests for BatchRecordingMixin.get_incomplete_batches."""

    def test_returns_draft_batches(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-draft", coordination_token=leader_coordination_token(factory, "run-1"))

        incomplete = factory.execution.get_incomplete_batches("run-1")

        assert len(incomplete) == 1
        assert incomplete[0].batch_id == "b-draft"

    def test_returns_executing_batches(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-exec", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.update_batch_status("b-exec", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "b-exec"))

        incomplete = factory.execution.get_incomplete_batches("run-1")

        assert len(incomplete) == 1
        assert incomplete[0].batch_id == "b-exec"
        assert incomplete[0].status == BatchStatus.EXECUTING

    def test_returns_failed_batches(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-fail", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.complete_batch("b-fail", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-fail"))

        incomplete = factory.execution.get_incomplete_batches("run-1")

        assert len(incomplete) == 1
        assert incomplete[0].batch_id == "b-fail"
        assert incomplete[0].status == BatchStatus.FAILED

    def test_excludes_completed_batches(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-done", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.complete_batch("b-done", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-done"))

        incomplete = factory.execution.get_incomplete_batches("run-1")

        assert len(incomplete) == 0

    def test_mixed_statuses(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-draft", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-exec", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-fail", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-done", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.update_batch_status("b-exec", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "b-exec"))
        factory.execution.complete_batch("b-fail", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-fail"))
        factory.execution.complete_batch("b-done", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-done"))

        incomplete = factory.execution.get_incomplete_batches("run-1")

        incomplete_ids = {b.batch_id for b in incomplete}
        assert incomplete_ids == {"b-draft", "b-exec", "b-fail"}

    def test_empty_for_run_with_no_batches(self):
        _db, factory = _setup()

        incomplete = factory.execution.get_incomplete_batches("run-1")

        assert incomplete == []


# ---------------------------------------------------------------------------
# get_batch_members (ordering)
# ---------------------------------------------------------------------------


class TestGetBatchMembers:
    """Tests for BatchRecordingMixin.get_batch_members ordering."""

    def test_returns_members_ordered_by_ordinal(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-1", "tok-2", ordinal=10)
        _add_batch_member(factory, "b-1", "tok-1", ordinal=5)

        members = factory.execution.get_batch_members("b-1")

        assert len(members) == 2
        assert members[0].token_id == "tok-1"
        assert members[0].ordinal == 5
        assert members[1].token_id == "tok-2"
        assert members[1].ordinal == 10

    def test_empty_for_batch_with_no_members(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-empty", coordination_token=leader_coordination_token(factory, "run-1"))

        members = factory.execution.get_batch_members("b-empty")

        assert members == []

    def test_members_only_from_specified_batch(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-2", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-1", "tok-1", ordinal=0)
        _add_batch_member(factory, "b-2", "tok-2", ordinal=0)

        members_b1 = factory.execution.get_batch_members("b-1")
        members_b2 = factory.execution.get_batch_members("b-2")

        assert len(members_b1) == 1
        assert members_b1[0].token_id == "tok-1"
        assert len(members_b2) == 1
        assert members_b2[0].token_id == "tok-2"


# ---------------------------------------------------------------------------
# get_all_batch_members_for_run
# ---------------------------------------------------------------------------


class TestGetAllBatchMembersForRun:
    """Tests for BatchRecordingMixin.get_all_batch_members_for_run."""

    def test_returns_all_members_across_batches(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.create_batch("agg-1", batch_id="b-2", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-1", "tok-1", ordinal=0)
        _add_batch_member(factory, "b-2", "tok-2", ordinal=0)

        all_members = factory.execution.get_all_batch_members_for_run("run-1")

        assert len(all_members) == 2
        token_ids = {m.token_id for m in all_members}
        assert token_ids == {"tok-1", "tok-2"}

    def test_empty_for_run_with_no_batch_members(self):
        _db, factory = _setup()

        all_members = factory.execution.get_all_batch_members_for_run("run-1")

        assert all_members == []

    def test_does_not_include_members_from_other_runs(self):
        db = make_landscape_db()
        factory = make_factory(db)

        # Run 1
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="run-1")
        factory.data_flow.register_node(
            plugin_name="csv",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            node_id="source-0",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.data_flow.register_node(
            plugin_name="aggregator",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id="agg-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.data_flow.create_row_with_token(
            "source-0",
            0,
            {"x": 1},
            row_id="row-1",
            source_row_index=0,
            ingest_sequence=0,
            token_id="tok-1",
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.execution.create_batch("agg-1", batch_id="b-1", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-1", "tok-1", ordinal=0)

        # Run 2
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="run-2")
        factory.data_flow.register_node(
            plugin_name="csv",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            node_id="source-0",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-2"),
        )
        factory.data_flow.register_node(
            plugin_name="aggregator",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id="agg-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-2"),
        )
        factory.data_flow.create_row_with_token(
            "source-0",
            0,
            {"x": 2},
            row_id="row-2",
            source_row_index=0,
            ingest_sequence=0,
            token_id="tok-2",
            coordination_token=leader_coordination_token(factory, "run-2"),
        )
        factory.execution.create_batch("agg-1", batch_id="b-2", coordination_token=leader_coordination_token(factory, "run-2"))
        _add_batch_member(factory, "b-2", "tok-2", ordinal=0)

        run1_members = factory.execution.get_all_batch_members_for_run("run-1")
        run2_members = factory.execution.get_all_batch_members_for_run("run-2")

        assert len(run1_members) == 1
        assert run1_members[0].token_id == "tok-1"
        assert len(run2_members) == 1
        assert run2_members[0].token_id == "tok-2"


class TestBatchRunOwnership:
    """Cross-run batch link prevention for audit integrity."""

    def test_record_token_outcome_rejects_batch_from_different_run(self):
        """Token outcomes must not point at batches from another run."""
        _db, factory = _setup_two_runs_with_batch_integrity_records()
        factory.execution.create_batch("agg-1", batch_id="batch-A", coordination_token=leader_coordination_token(factory, "run-A"))
        member_token = leader_coordination_token(factory, "run-B").membership
        work_item = _work_item_for_token(factory, "tok-B")

        with pytest.raises(AuditIntegrityError):
            factory.data_flow.record_token_outcome(
                ref=TokenRef(token_id="tok-B", run_id="run-B"),
                outcome=None,
                path=TerminalPath.BUFFERED,
                batch_id="batch-A",
                member_token=member_token,
                work_item=work_item,
            )


# ---------------------------------------------------------------------------
# retry_batch
# ---------------------------------------------------------------------------


class TestRetryBatch:
    """Tests for BatchRecordingMixin.retry_batch."""

    def test_creates_new_batch_with_incremented_attempt(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-orig", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-orig", "tok-1", ordinal=0)
        _add_batch_member(factory, "b-orig", "tok-2", ordinal=1)
        factory.execution.complete_batch("b-orig", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-orig"))

        retried = factory.execution.retry_batch("b-orig", coordination_token=_batch_token(factory, "b-orig"))

        assert retried.batch_id != "b-orig"
        assert retried.attempt == 1
        assert retried.run_id == "run-1"
        assert retried.aggregation_node_id == "agg-1"
        assert retried.status == BatchStatus.DRAFT

    def test_copies_members_to_new_batch(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-orig", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-orig", "tok-1", ordinal=0)
        _add_batch_member(factory, "b-orig", "tok-2", ordinal=1)
        factory.execution.complete_batch("b-orig", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-orig"))

        retried = factory.execution.retry_batch("b-orig", coordination_token=_batch_token(factory, "b-orig"))

        members = factory.execution.get_batch_members(retried.batch_id)
        assert len(members) == 2
        token_ids = [m.token_id for m in members]
        assert "tok-1" in token_ids
        assert "tok-2" in token_ids

    def test_raises_for_non_failed_batch_draft(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-draft", coordination_token=leader_coordination_token(factory, "run-1"))

        with pytest.raises(AuditIntegrityError):
            factory.execution.retry_batch("b-draft", coordination_token=_batch_token(factory, "b-draft"))

    def test_raises_for_non_failed_batch_completed(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-done", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.complete_batch("b-done", BatchStatus.COMPLETED, coordination_token=_batch_token(factory, "b-done"))

        with pytest.raises(AuditIntegrityError):
            factory.execution.retry_batch("b-done", coordination_token=_batch_token(factory, "b-done"))

    def test_raises_for_non_failed_batch_executing(self):
        _db, factory = _setup()
        factory.execution.create_batch("agg-1", batch_id="b-exec", coordination_token=leader_coordination_token(factory, "run-1"))
        factory.execution.update_batch_status("b-exec", BatchStatus.EXECUTING, coordination_token=_batch_token(factory, "b-exec"))

        with pytest.raises(AuditIntegrityError):
            factory.execution.retry_batch("b-exec", coordination_token=_batch_token(factory, "b-exec"))

    def test_raises_for_nonexistent_batch(self):
        _db, factory = _setup()

        with pytest.raises(AuditIntegrityError):
            factory.execution.retry_batch("nonexistent", coordination_token=_batch_token(factory, "nonexistent"))

    def test_retry_increments_from_previous_attempt(self):
        _db, factory = _setup_with_token()
        factory.execution.create_batch(
            "agg-1", batch_id="b-orig", attempt=0, coordination_token=leader_coordination_token(factory, "run-1")
        )
        _add_batch_member(factory, "b-orig", "tok-1", ordinal=0)
        factory.execution.complete_batch("b-orig", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-orig"))

        retry1 = factory.execution.retry_batch("b-orig", coordination_token=_batch_token(factory, "b-orig"))
        assert retry1.attempt == 1

        # Fail the retry and retry again
        factory.execution.complete_batch(retry1.batch_id, BatchStatus.FAILED, coordination_token=_batch_token(factory, retry1.batch_id))
        retry2 = factory.execution.retry_batch(retry1.batch_id, coordination_token=_batch_token(factory, retry1.batch_id))
        assert retry2.attempt == 2

    def test_retry_is_idempotent(self):
        """Retrying the same failed batch twice returns the same retry batch."""
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-orig", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-orig", "tok-1", ordinal=0)
        _add_batch_member(factory, "b-orig", "tok-2", ordinal=1)
        factory.execution.complete_batch("b-orig", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-orig"))

        first_retry = factory.execution.retry_batch("b-orig", coordination_token=_batch_token(factory, "b-orig"))
        second_retry = factory.execution.retry_batch("b-orig", coordination_token=_batch_token(factory, "b-orig"))

        assert first_retry.batch_id == second_retry.batch_id
        assert first_retry.attempt == second_retry.attempt == 1

        # Only one retry batch exists (not two)
        all_batches = factory.execution.get_batches("run-1")
        attempt_1_batches = [b for b in all_batches if b.attempt == 1]
        assert len(attempt_1_batches) == 1

    def test_retry_idempotency_across_recovery_cycles(self):
        """Simulates crash-recovery-crash-recovery creating only one retry."""
        _db, factory = _setup_with_token()
        factory.execution.create_batch("agg-1", batch_id="b-orig", coordination_token=leader_coordination_token(factory, "run-1"))
        _add_batch_member(factory, "b-orig", "tok-1", ordinal=0)
        factory.execution.complete_batch("b-orig", BatchStatus.FAILED, coordination_token=_batch_token(factory, "b-orig"))

        # First recovery cycle creates retry batch
        retry1 = factory.execution.retry_batch("b-orig", coordination_token=_batch_token(factory, "b-orig"))

        # Simulate crash: retry batch stays as DRAFT
        # Second recovery cycle tries to retry the same failed batch
        retry2 = factory.execution.retry_batch("b-orig", coordination_token=_batch_token(factory, "b-orig"))

        # Third recovery cycle — same thing
        retry3 = factory.execution.retry_batch("b-orig", coordination_token=_batch_token(factory, "b-orig"))

        # All three calls return the same batch
        assert retry1.batch_id == retry2.batch_id == retry3.batch_id
        assert retry1.attempt == 1

        # Members were only copied once
        members = factory.execution.get_batch_members(retry1.batch_id)
        assert len(members) == 1
        assert members[0].token_id == "tok-1"


# ---------------------------------------------------------------------------
# register_artifact
# ---------------------------------------------------------------------------


class TestRegisterArtifact:
    """Tests for BatchRecordingMixin.register_artifact."""

    def test_creates_artifact_with_generated_id(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )

        artifact = _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/result.csv",
            content_hash="sha256:abc123",
            size_bytes=1024,
        )

        assert artifact.artifact_id is not None
        assert isinstance(artifact.artifact_id, str)
        assert len(artifact.artifact_id) > 0

    def test_uses_provided_artifact_id(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )

        artifact = _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/result.csv",
            content_hash="sha256:abc123",
            size_bytes=1024,
            artifact_id="art-42",
        )

        assert artifact.artifact_id == "art-42"

    def test_stores_all_fields(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )

        artifact = _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="json",
            path="/output/data.json",
            content_hash="sha256:def456",
            size_bytes=2048,
        )

        assert artifact.run_id == "run-1"
        assert artifact.produced_by_state_id == "state-1"
        assert artifact.sink_node_id == "sink-0"
        assert artifact.artifact_type == "json"
        assert artifact.path_or_uri == "/output/data.json"
        assert artifact.content_hash == "sha256:def456"
        assert artifact.size_bytes == 2048

    def test_created_at_is_set(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )

        artifact = _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/result.csv",
            content_hash="sha256:abc",
            size_bytes=512,
        )

        assert artifact.created_at is not None

    def test_idempotency_key_default_none(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )

        artifact = _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/result.csv",
            content_hash="sha256:abc",
            size_bytes=512,
        )

        assert artifact.idempotency_key is None

    def test_idempotency_key_explicit(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )

        artifact = _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/result.csv",
            content_hash="sha256:abc",
            size_bytes=512,
            idempotency_key="idem-key-1",
        )

        assert artifact.idempotency_key == "idem-key-1"

    def test_identical_idempotent_retry_returns_original_artifact(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        values = {
            "run_id": "run-1",
            "state_id": "state-1",
            "sink_node_id": "sink-0",
            "artifact_type": "csv",
            "path": "/output/result.csv",
            "content_hash": "sha256:abc",
            "size_bytes": 512,
            "idempotency_key": "run-1:row-1:csv_sink",
        }

        first = _register_artifact(factory, **values, artifact_id="artifact-first")
        retried = _register_artifact(factory, **values, artifact_id="artifact-retry-proposal")

        assert retried == first
        assert retried.artifact_id == "artifact-first"
        assert len(factory.execution.get_artifacts("run-1")) == 1

    @pytest.mark.parametrize(
        ("field", "divergent_value"),
        [
            ("state_id", "state-2"),
            ("sink_node_id", "sink-1"),
            ("artifact_type", "json"),
            ("path", "/output/different.csv"),
            ("content_hash", "sha256:different"),
            ("size_bytes", 513),
        ],
    )
    def test_idempotency_key_reuse_with_divergent_effect_fails_closed(self, field: str, divergent_value: str | int):
        _db, factory = _setup_with_sink()
        factory.data_flow.register_node(
            plugin_name="json_sink",
            node_type=NodeType.SINK,
            plugin_version="1.0",
            config={},
            node_id="sink-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "first"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        factory.execution.begin_node_state(
            "tok-2",
            "source-0",
            1,
            {"data": "second"},
            state_id="state-2",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        values: dict[str, str | int] = {
            "run_id": "run-1",
            "state_id": "state-1",
            "sink_node_id": "sink-0",
            "artifact_type": "csv",
            "path": "/output/result.csv",
            "content_hash": "sha256:abc",
            "size_bytes": 512,
            "idempotency_key": "run-1:row-1:csv_sink",
        }
        original = _register_artifact(
            factory,
            run_id=str(values["run_id"]),
            state_id=str(values["state_id"]),
            sink_node_id=str(values["sink_node_id"]),
            artifact_type=str(values["artifact_type"]),
            path=str(values["path"]),
            content_hash=str(values["content_hash"]),
            size_bytes=int(values["size_bytes"]),
            idempotency_key=str(values["idempotency_key"]),
        )
        values[field] = divergent_value

        with pytest.raises(AuditIntegrityError, match=field):
            _register_artifact(
                factory,
                run_id=str(values["run_id"]),
                state_id=str(values["state_id"]),
                sink_node_id=str(values["sink_node_id"]),
                artifact_type=str(values["artifact_type"]),
                path=str(values["path"]),
                content_hash=str(values["content_hash"]),
                size_bytes=int(values["size_bytes"]),
                idempotency_key=str(values["idempotency_key"]),
            )

        assert factory.execution.get_artifacts("run-1") == [original]

    def test_null_idempotency_keys_remain_independent(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        values = {
            "run_id": "run-1",
            "state_id": "state-1",
            "sink_node_id": "sink-0",
            "artifact_type": "csv",
            "path": "/output/result.csv",
            "content_hash": "sha256:abc",
            "size_bytes": 512,
        }

        first = _register_artifact(factory, **values)
        second = _register_artifact(factory, **values)

        assert first.artifact_id != second.artifact_id
        assert len(factory.execution.get_artifacts("run-1")) == 2

    def test_same_idempotency_key_is_independent_between_runs(self):
        _db, factory = _setup_two_runs_with_batch_integrity_records()
        values = {
            "sink_node_id": "sink-0",
            "artifact_type": "csv",
            "path": "/output/result.csv",
            "content_hash": "sha256:abc",
            "size_bytes": 512,
            "idempotency_key": "opaque-logical-effect",
        }

        run_a = _register_artifact(factory, run_id="run-A", state_id="state-A", **values)
        run_b = _register_artifact(factory, run_id="run-B", state_id="state-B", **values)

        assert run_a.artifact_id != run_b.artifact_id
        assert factory.execution.get_artifacts("run-A") == [run_a]
        assert factory.execution.get_artifacts("run-B") == [run_b]

    def test_fault_before_outer_commit_rolls_back_idempotency_reservation(self):
        db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        values = {
            "run_id": "run-1",
            "state_id": "state-1",
            "sink_node_id": "sink-0",
            "artifact_type": "csv",
            "path": "/output/result.csv",
            "content_hash": "sha256:abc",
            "size_bytes": 512,
            "idempotency_key": "run-1:row-1:csv_sink",
        }

        with pytest.raises(RuntimeError, match="fault before commit"), db.write_connection() as conn:
            _register_artifact(factory, **values, conn=conn)
            raise RuntimeError("fault before commit")

        committed = _register_artifact(factory, **values)
        assert factory.execution.get_artifacts("run-1") == [committed]

    def test_retry_after_response_loss_after_commit_returns_committed_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        values = {
            "run_id": "run-1",
            "state_id": "state-1",
            "sink_node_id": "sink-0",
            "artifact_type": "csv",
            "path": "/output/result.csv",
            "content_hash": "sha256:abc",
            "size_bytes": 512,
            "idempotency_key": "run-1:row-1:csv_sink",
        }
        real_write_connection = run_coordination_repository.begin_write

        @contextmanager
        def _commit_then_lose_response(engine) -> Iterator[Connection]:
            with real_write_connection(engine) as conn:
                yield conn
            raise RuntimeError("response lost after commit before return to caller")

        monkeypatch.setattr(run_coordination_repository, "begin_write", _commit_then_lose_response)
        with pytest.raises(RuntimeError, match="response lost after commit"):
            _register_artifact(factory, **values, artifact_id="artifact-committed-before-response-loss")

        committed = factory.execution.get_artifacts("run-1")
        assert len(committed) == 1
        monkeypatch.setattr(run_coordination_repository, "begin_write", real_write_connection)
        retried = _register_artifact(factory, **values, artifact_id="artifact-retry-after-response-loss")
        assert retried.artifact_id == "artifact-committed-before-response-loss"
        assert retried.created_at == committed[0].created_at
        assert factory.execution.get_artifacts("run-1") == [retried]

    def test_rejects_producer_state_from_different_run(self):
        """Artifacts must not be attributed to a node state from another run."""
        _db, factory = _setup_two_runs_with_batch_integrity_records()

        with pytest.raises(AuditIntegrityError):
            _register_artifact(
                factory,
                run_id="run-A",
                state_id="state-B",
                sink_node_id="sink-0",
                artifact_type="csv",
                path="/output/foreign-state.csv",
                content_hash="sha256:foreign",
                size_bytes=1,
                artifact_id="artifact-foreign-state",
            )


# ---------------------------------------------------------------------------
# get_artifacts
# ---------------------------------------------------------------------------


class TestGetArtifacts:
    """Tests for BatchRecordingMixin.get_artifacts."""

    def test_lists_all_artifacts_for_run(self):
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/a.csv",
            content_hash="sha256:a",
            size_bytes=100,
            artifact_id="art-1",
        )
        _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="json",
            path="/output/b.json",
            content_hash="sha256:b",
            size_bytes=200,
            artifact_id="art-2",
        )

        artifacts = factory.execution.get_artifacts("run-1")

        assert len(artifacts) == 2
        art_ids = {a.artifact_id for a in artifacts}
        assert art_ids == {"art-1", "art-2"}

    def test_empty_for_run_with_no_artifacts(self):
        _db, factory = _setup()

        artifacts = factory.execution.get_artifacts("run-1")

        assert artifacts == []

    def test_filter_by_sink_node_id(self):
        _db, factory = _setup_with_sink()
        factory.data_flow.register_node(
            plugin_name="json_sink",
            node_type=NodeType.SINK,
            plugin_version="1.0",
            config={},
            node_id="sink-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/a.csv",
            content_hash="sha256:a",
            size_bytes=100,
            artifact_id="art-csv",
        )
        _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-1",
            artifact_type="json",
            path="/output/b.json",
            content_hash="sha256:b",
            size_bytes=200,
            artifact_id="art-json",
        )

        csv_artifacts = factory.execution.get_artifacts("run-1", sink_node_id="sink-0")
        json_artifacts = factory.execution.get_artifacts("run-1", sink_node_id="sink-1")

        assert len(csv_artifacts) == 1
        assert csv_artifacts[0].artifact_id == "art-csv"
        assert len(json_artifacts) == 1
        assert json_artifacts[0].artifact_id == "art-json"

    def test_does_not_return_artifacts_from_other_runs(self):
        db = make_landscape_db()
        factory = make_factory(db)

        # Run 1
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="run-1")
        factory.data_flow.register_node(
            plugin_name="csv",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            node_id="source-0",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.data_flow.register_node(
            plugin_name="csv_sink",
            node_type=NodeType.SINK,
            plugin_version="1.0",
            config={},
            node_id="sink-0",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.data_flow.create_row_with_token(
            "source-0",
            0,
            {"x": 1},
            row_id="row-1",
            source_row_index=0,
            ingest_sequence=0,
            token_id="tok-1",
            coordination_token=leader_coordination_token(factory, "run-1"),
        )
        factory.execution.begin_node_state(
            "tok-1", "source-0", 0, {"x": 1}, state_id="s-1", member_token=leader_coordination_token(factory, "run-1").membership
        )
        _register_artifact(
            factory,
            run_id="run-1",
            state_id="s-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/r1.csv",
            content_hash="sha256:r1",
            size_bytes=100,
            artifact_id="art-r1",
        )

        # Run 2
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="run-2")
        factory.data_flow.register_node(
            plugin_name="csv",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            node_id="source-0",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-2"),
        )
        factory.data_flow.register_node(
            plugin_name="csv_sink",
            node_type=NodeType.SINK,
            plugin_version="1.0",
            config={},
            node_id="sink-0",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "run-2"),
        )
        factory.data_flow.create_row_with_token(
            "source-0",
            0,
            {"x": 2},
            row_id="row-2",
            source_row_index=0,
            ingest_sequence=0,
            token_id="tok-2",
            coordination_token=leader_coordination_token(factory, "run-2"),
        )
        factory.execution.begin_node_state(
            "tok-2", "source-0", 0, {"x": 2}, state_id="s-2", member_token=leader_coordination_token(factory, "run-2").membership
        )
        _register_artifact(
            factory,
            run_id="run-2",
            state_id="s-2",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/r2.csv",
            content_hash="sha256:r2",
            size_bytes=200,
            artifact_id="art-r2",
        )

        run1_arts = factory.execution.get_artifacts("run-1")
        run2_arts = factory.execution.get_artifacts("run-2")

        assert len(run1_arts) == 1
        assert run1_arts[0].artifact_id == "art-r1"
        assert len(run2_arts) == 1
        assert run2_arts[0].artifact_id == "art-r2"

    def test_deterministic_ordering_by_created_at_then_artifact_id(self):
        """Bug 6kno: get_artifacts() must return deterministic order for export signing."""
        _db, factory = _setup_with_sink()
        factory.execution.begin_node_state(
            "tok-1",
            "source-0",
            0,
            {"data": "test"},
            state_id="state-1",
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        # Register multiple artifacts
        _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/c.csv",
            content_hash="sha256:c",
            size_bytes=300,
            artifact_id="art-c",
        )
        _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="csv",
            path="/output/a.csv",
            content_hash="sha256:a",
            size_bytes=100,
            artifact_id="art-a",
        )
        _register_artifact(
            factory,
            run_id="run-1",
            state_id="state-1",
            sink_node_id="sink-0",
            artifact_type="json",
            path="/output/b.json",
            content_hash="sha256:b",
            size_bytes=200,
            artifact_id="art-b",
        )

        # Call twice — must be identical
        artifacts_first = factory.execution.get_artifacts("run-1")
        artifacts_second = factory.execution.get_artifacts("run-1")

        ids_first = [a.artifact_id for a in artifacts_first]
        ids_second = [a.artifact_id for a in artifacts_second]
        assert ids_first == ids_second
        assert len(ids_first) == 3


class TestAddBatchMemberGuardedPostgresLockOrder:
    """PostgreSQL parent-lock acquisition order for batch membership (elspeth-a580f44add).

    Outcome recording (``TokenOutcomeRepository.lock_token_outcome_dependencies``)
    locks ``tokens`` FOR UPDATE first and then takes an implicit FK ``KEY SHARE``
    on ``batches`` when it writes a batch-scoped outcome row.  Membership must
    acquire its parent locks in the same token-first order; locking the batch
    row first while the membership INSERT waits on the token FK lock deadlocks
    against a concurrent outcome write and aborts one audit transaction.
    """

    @staticmethod
    def _run_guarded_add(statements: list[str]):
        """Drive add_batch_member_guarded against a statement-recording fake."""
        from types import SimpleNamespace
        from typing import cast

        from sqlalchemy.dialects import postgresql

        from elspeth.core.landscape.execution.batches import add_batch_member_guarded

        token_row = SimpleNamespace(token_id="tok-1")
        batch_row = SimpleNamespace(run_id="run-1", status="draft")
        inserted_row = SimpleNamespace(batch_id="batch-1", run_id="run-1", token_id="tok-1", ordinal=0)

        class _RecordedResult:
            def __init__(self, rows: list[object]) -> None:
                self._rows = rows

            def fetchone(self) -> object | None:
                return self._rows[0] if self._rows else None

            def fetchall(self) -> list[object]:
                return list(self._rows)

        class _RecordingPostgresConnection:
            dialect = type("_Dialect", (), {"name": "postgresql"})()

            def execute(self, statement: object) -> _RecordedResult:
                compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
                sql = " ".join(str(compiled).upper().split())
                statements.append(sql)
                if sql.startswith("INSERT INTO BATCH_MEMBERS"):
                    return _RecordedResult([inserted_row])
                if sql.startswith("SELECT") and "FROM TOKENS" in sql:
                    return _RecordedResult([token_row])
                if sql.startswith("SELECT") and "FROM BATCHES" in sql:
                    return _RecordedResult([batch_row])
                raise AssertionError(f"unexpected statement: {sql}")

        return add_batch_member_guarded(
            cast(Connection, _RecordingPostgresConnection()),
            batch_id="batch-1",
            token_id="tok-1",
            ordinal=0,
        )

    def test_postgres_membership_locks_token_before_batch(self):
        """The token row lock must be acquired before the batch FOR UPDATE."""
        statements: list[str] = []
        member = self._run_guarded_add(statements)

        assert member.batch_id == "batch-1"
        assert member.token_id == "tok-1"

        token_locks = [i for i, sql in enumerate(statements) if sql.startswith("SELECT") and "FROM TOKENS" in sql and "FOR UPDATE" in sql]
        batch_locks = [i for i, sql in enumerate(statements) if sql.startswith("SELECT") and "FROM BATCHES" in sql and "FOR UPDATE" in sql]

        assert token_locks, (
            "PostgreSQL membership path must lock the token row FOR UPDATE before "
            "anything else (token-first order shared with outcome recording); "
            f"observed statements: {statements}"
        )
        assert batch_locks, f"PostgreSQL membership path must keep its batch FOR UPDATE guard; observed statements: {statements}"
        assert token_locks[0] < batch_locks[0], (
            "PostgreSQL membership path must acquire the token lock BEFORE the batch "
            "FOR UPDATE — batch-first order deadlocks against token-first outcome "
            f"recording; observed statements: {statements}"
        )

    def test_postgres_membership_keeps_draft_guard_and_insert_last(self):
        """Reordering the locks must not drop the DRAFT guard or reorder the INSERT."""
        statements: list[str] = []
        self._run_guarded_add(statements)

        insert_indexes = [i for i, sql in enumerate(statements) if sql.startswith("INSERT INTO BATCH_MEMBERS")]
        batch_locks = [i for i, sql in enumerate(statements) if sql.startswith("SELECT") and "FROM BATCHES" in sql and "FOR UPDATE" in sql]
        assert len(insert_indexes) == 1
        assert batch_locks and batch_locks[0] < insert_indexes[0], (
            f"batch FOR UPDATE status guard must still precede the membership INSERT; observed statements: {statements}"
        )

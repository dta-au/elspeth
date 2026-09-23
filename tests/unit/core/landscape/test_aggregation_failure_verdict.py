"""A batch transform's FAILED verdict is recorded whole, once, and is final (operator ruling 2026-09-23).

``ExecutionRepository.complete_aggregation_failure`` writes the verdict in ONE
leader-fenced transaction: one ``transform_errors`` row per member, the DIVERT
``routing_event`` of a named on_error sink, the flush node_state FAILED with the
reason, and the batch FAILED. ``recorded_failure_verdict_condition`` is the one
predicate for "this batch has a final verdict": ``get_incomplete_batches``
excludes such a batch (nothing to retry), ``retry_batch`` refuses it, and
``BarrierRestoreReadModel.list_recorded_aggregation_failures`` hands its
still-BLOCKED members to resume, proved whole.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Table, func, select, update
from sqlalchemy.sql import Executable

from elspeth.contracts import BatchStatus, NodeStateStatus, NodeType, RoutingMode, TriggerType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.errors import AuditIntegrityError, TransformErrorReason
from elspeth.core.canonical import canonical_json
from elspeth.core.landscape.data_flow.outcomes import record_buffered_outcome_guarded
from elspeth.core.landscape.execution.batches import add_batch_member_guarded
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import routing_events_table, transform_errors_table
from elspeth.testing import make_pipeline_row
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run, register_test_node

_REASON: TransformErrorReason = {"reason": "batch_failed", "error": "flush failed"}
_REASON_JSON = canonical_json(_REASON)
_DIVERT_EDGE = "edge-agg-error"
_TOKENS = ("tok-0", "tok-1")


def _setup() -> RecorderSetup:
    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    register_test_node(setup.data_flow, setup.run_id, "agg-1", node_type=NodeType.AGGREGATION, plugin_name="aggregator")
    register_test_node(setup.data_flow, setup.run_id, "sink-q", node_type=NodeType.SINK, plugin_name="csv")
    setup.data_flow.register_edge(
        "agg-1", "sink-q", "__error_agg__", RoutingMode.DIVERT, coordination_token=setup.coordination_token, edge_id=_DIVERT_EDGE
    )
    for ordinal, token_id in enumerate(_TOKENS):
        setup.data_flow.create_row_with_token(
            setup.source_node_id,
            ordinal,
            {"value": ordinal + 1},
            row_id=f"row-{ordinal}",
            source_row_index=ordinal,
            ingest_sequence=ordinal,
            coordination_token=setup.coordination_token,
            token_id=token_id,
        )
    return setup


def _buffered_batch(setup: RecorderSetup, batch_id: str = "batch-1") -> None:
    """A DRAFT batch holding both tokens, each with its live BUFFERED acceptance."""
    setup.execution.create_batch("agg-1", batch_id=batch_id, coordination_token=setup.coordination_token)
    with fenced_leader_transaction(setup.db.engine, token=setup.coordination_token, window_seconds=300, verb="test_verdict_setup") as conn:
        for ordinal, token_id in enumerate(_TOKENS):
            add_batch_member_guarded(conn, batch_id=batch_id, token_id=token_id, ordinal=ordinal, expected_run_id=setup.run_id)
            record_buffered_outcome_guarded(conn, run_id=setup.run_id, token_id=token_id, batch_id=batch_id, recorded_at=datetime.now(UTC))


def _flush_state(setup: RecorderSetup, batch_id: str, state_id: str, *, attempt: int = 0) -> str:
    """Open the flush node_state and move the batch to EXECUTING, as ``execute_flush`` does."""
    state = setup.execution.begin_node_state(
        "tok-0", "agg-1", 1, {"batch_rows": []}, state_id=state_id, attempt=attempt, member_token=setup.coordination_token.membership
    )
    setup.execution.update_batch_status(batch_id, BatchStatus.EXECUTING, coordination_token=setup.coordination_token)
    return state.state_id


def _record_verdict(
    setup: RecorderSetup, batch_id: str, state_id: str, *, destination: str, divert_edge_id: str | None, members: tuple[str, ...] = _TOKENS
) -> None:
    setup.execution.complete_aggregation_failure(
        batch_id=batch_id,
        coordination_token=setup.coordination_token,
        aggregation_node_id="agg-1",
        state_id=state_id,
        trigger_type=TriggerType.END_OF_SOURCE,
        members=tuple((TokenRef(token_id=token_id, run_id=setup.run_id), make_pipeline_row({"value": 1})) for token_id in members),
        reason=_REASON,
        destination=destination,
        divert_edge_id=divert_edge_id,
        duration_ms=1.0,
    )


def _count(setup: RecorderSetup, table: Table) -> int:
    with setup.db.read_only_connection() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def _nothing_recorded(setup: RecorderSetup, batch_id: str, state_id: str) -> None:
    assert _count(setup, transform_errors_table) == 0
    assert _count(setup, routing_events_table) == 0
    assert setup.execution.get_node_state(state_id).status is NodeStateStatus.OPEN
    assert setup.execution.get_batch(batch_id).status is BatchStatus.EXECUTING


_ARMS = [
    pytest.param("quarantine", _DIVERT_EDGE, 1, id="named-sink"),
    pytest.param("discard", None, 0, id="discard"),
]


class TestTheVerdictIsOneTransaction:
    @pytest.mark.parametrize(("destination", "divert_edge_id", "diverts"), _ARMS)
    def test_the_verdict_is_written_whole(self, destination: str, divert_edge_id: str | None, diverts: int) -> None:
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")

        _record_verdict(setup, "batch-1", state_id, destination=destination, divert_edge_id=divert_edge_id)

        with setup.db.read_only_connection() as conn:
            errors = conn.execute(
                select(transform_errors_table.c.token_id, transform_errors_table.c.error_details_json, transform_errors_table.c.destination)
                .where(transform_errors_table.c.transform_id == "agg-1")
                .order_by(transform_errors_table.c.token_id)
            ).all()
            routes = conn.execute(
                select(routing_events_table.c.edge_id, routing_events_table.c.mode, routing_events_table.c.state_id)
            ).all()
        assert [tuple(row) for row in errors] == [(token_id, _REASON_JSON, destination) for token_id in _TOKENS]
        assert [tuple(row) for row in routes] == ([(_DIVERT_EDGE, RoutingMode.DIVERT.value, state_id)] if diverts else [])
        state = setup.execution.get_node_state(state_id)
        assert state.status is NodeStateStatus.FAILED
        assert json.loads(state.error_json) == _REASON
        batch = setup.execution.get_batch("batch-1")
        assert batch.status is BatchStatus.FAILED
        assert batch.aggregation_state_id == state_id
        assert batch.trigger_type is TriggerType.END_OF_SOURCE

    @pytest.mark.parametrize(("destination", "divert_edge_id", "diverts"), _ARMS)
    def test_a_failure_at_the_last_write_rolls_the_whole_verdict_back(
        self, monkeypatch: pytest.MonkeyPatch, destination: str, divert_edge_id: str | None, diverts: int
    ) -> None:
        """No crash can leave the transform_errors rows (or the DIVERT) without the FAILED batch."""
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")
        real_complete_batch = setup.execution.batches.complete_batch_on

        def fail_after_batch_completion(*args: Any, **kwargs: Any) -> Any:
            real_complete_batch(*args, **kwargs)
            raise RuntimeError("injected failure after the batch write")

        monkeypatch.setattr(setup.execution.batches, "complete_batch_on", fail_after_batch_completion)

        with pytest.raises(RuntimeError, match="injected failure after the batch write"):
            _record_verdict(setup, "batch-1", state_id, destination=destination, divert_edge_id=divert_edge_id)

        _nothing_recorded(setup, "batch-1", state_id)
        assert [batch.batch_id for batch in setup.execution.get_incomplete_batches(setup.run_id)] == ["batch-1"]

    @pytest.mark.parametrize(
        ("destination", "divert_edge_id"),
        [pytest.param("discard", _DIVERT_EDGE, id="discard-with-an-edge"), pytest.param("quarantine", None, id="named-without-an-edge")],
    )
    def test_a_destination_that_disagrees_with_its_divert_edge_is_refused(self, destination: str, divert_edge_id: str | None) -> None:
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")

        with pytest.raises(AuditIntegrityError, match="disagrees with its DIVERT edge"):
            _record_verdict(setup, "batch-1", state_id, destination=destination, divert_edge_id=divert_edge_id)

        _nothing_recorded(setup, "batch-1", state_id)

    def test_members_that_are_not_the_exact_ordered_membership_are_refused(self) -> None:
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")

        with pytest.raises(AuditIntegrityError, match="exact ordered batch membership"):
            _record_verdict(setup, "batch-1", state_id, destination="discard", divert_edge_id=None, members=("tok-1", "tok-0"))

        _nothing_recorded(setup, "batch-1", state_id)

    def test_a_batch_that_is_not_executing_is_refused(self) -> None:
        setup = _setup()
        _buffered_batch(setup)
        state = setup.execution.begin_node_state(
            "tok-0", "agg-1", 1, {"batch_rows": []}, state_id="state-1", member_token=setup.coordination_token.membership
        )

        with pytest.raises(AuditIntegrityError, match="an OPEN state and an EXECUTING batch"):
            _record_verdict(setup, "batch-1", state.state_id, destination="discard", divert_edge_id=None)

        assert _count(setup, transform_errors_table) == 0
        assert setup.execution.get_batch("batch-1").status is BatchStatus.DRAFT

    def test_a_second_verdict_for_the_same_flush_is_refused(self) -> None:
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")
        _record_verdict(setup, "batch-1", state_id, destination="discard", divert_edge_id=None)

        with pytest.raises(AuditIntegrityError, match="an OPEN state and an EXECUTING batch"):
            _record_verdict(setup, "batch-1", state_id, destination="discard", divert_edge_id=None)

        assert _count(setup, transform_errors_table) == len(_TOKENS)


class TestTheVerdictIsFinal:
    def test_a_recorded_verdict_is_not_incomplete_and_is_never_retried(self) -> None:
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")
        _record_verdict(setup, "batch-1", state_id, destination="quarantine", divert_edge_id=_DIVERT_EDGE)

        assert setup.execution.get_incomplete_batches(setup.run_id) == []
        with pytest.raises(AuditIntegrityError, match="carries a recorded FAILED verdict, which is final"):
            setup.execution.retry_batch("batch-1", coordination_token=setup.coordination_token)

    def test_a_failed_flush_without_a_verdict_is_retried(self) -> None:
        """Control: the plugin raised (or resume found the batch EXECUTING) — no verdict, so it is incomplete and retryable."""
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")
        setup.execution.complete_batch(
            "batch-1",
            BatchStatus.FAILED,
            trigger_type=TriggerType.END_OF_SOURCE,
            state_id=state_id,
            coordination_token=setup.coordination_token,
        )

        assert [batch.batch_id for batch in setup.execution.get_incomplete_batches(setup.run_id)] == ["batch-1"]
        retry = setup.execution.retry_batch("batch-1", coordination_token=setup.coordination_token)
        assert retry.retry_of_batch_id == "batch-1"

    def test_the_verdict_belongs_to_the_end_of_the_retry_chain(self) -> None:
        """A crashed attempt A was retried as B; B recorded the verdict.

        A shares B's members (a retry copies them), so A's members have
        transform_errors rows too — yet A is not a verdict: it has a retry.
        A stays incomplete (its idempotent retry returns B, keeping the chain
        edge), B is excluded, and only B is handed to resume.
        """
        setup = _setup()
        _buffered_batch(setup, "batch-a")
        crashed_state = _flush_state(setup, "batch-a", "state-a")
        setup.execution.complete_batch(
            "batch-a",
            BatchStatus.FAILED,
            trigger_type=TriggerType.END_OF_SOURCE,
            state_id=crashed_state,
            coordination_token=setup.coordination_token,
        )
        retry = setup.execution.retry_batch("batch-a", coordination_token=setup.coordination_token)
        verdict_state = _flush_state(setup, retry.batch_id, "state-b", attempt=1)
        _record_verdict(setup, retry.batch_id, verdict_state, destination="discard", divert_edge_id=None)

        assert [batch.batch_id for batch in setup.execution.get_incomplete_batches(setup.run_id)] == ["batch-a"]
        assert setup.execution.retry_batch("batch-a", coordination_token=setup.coordination_token).batch_id == retry.batch_id
        recorded = setup.factory.barrier_restore.list_recorded_aggregation_failures(
            setup.run_id, aggregation_node_id="agg-1", blocked_token_ids=list(_TOKENS)
        )
        assert [(verdict.batch_id, verdict.aggregation_state_id) for verdict in recorded] == [(retry.batch_id, verdict_state)]


class TestTheRestoreReaderProvesTheVerdictWhole:
    @pytest.mark.parametrize(("destination", "divert_edge_id", "diverts"), _ARMS)
    def test_a_verdict_with_blocked_members_is_returned_with_its_recorded_reason(
        self, destination: str, divert_edge_id: str | None, diverts: int
    ) -> None:
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")
        _record_verdict(setup, "batch-1", state_id, destination=destination, divert_edge_id=divert_edge_id)

        (verdict,) = setup.factory.barrier_restore.list_recorded_aggregation_failures(
            setup.run_id, aggregation_node_id="agg-1", blocked_token_ids=list(_TOKENS)
        )

        assert verdict.batch_id == "batch-1"
        assert verdict.aggregation_state_id == state_id
        assert verdict.member_token_ids == _TOKENS
        assert verdict.reason_json == _REASON_JSON
        assert verdict.destination == destination

    def test_members_no_longer_blocked_are_not_returned(self) -> None:
        """The disposition's complete_barrier already released them: nothing left to complete."""
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")
        _record_verdict(setup, "batch-1", state_id, destination="discard", divert_edge_id=None)

        assert (
            setup.factory.barrier_restore.list_recorded_aggregation_failures(
                setup.run_id, aggregation_node_id="agg-1", blocked_token_ids=[]
            )
            == ()
        )
        assert (
            setup.factory.barrier_restore.list_recorded_aggregation_failures(
                setup.run_id, aggregation_node_id="agg-1", blocked_token_ids=["tok-unrelated"]
            )
            == ()
        )

    def test_a_verdict_only_partly_blocked_is_corruption(self) -> None:
        """complete_barrier releases a batch's members all at once, never some."""
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")
        _record_verdict(setup, "batch-1", state_id, destination="discard", divert_edge_id=None)

        with pytest.raises(AuditIntegrityError, match="exact non-overlapping BLOCKED member set"):
            setup.factory.barrier_restore.list_recorded_aggregation_failures(
                setup.run_id, aggregation_node_id="agg-1", blocked_token_ids=["tok-0"]
            )

    @pytest.mark.parametrize(
        ("tamper", "match"),
        [
            pytest.param(
                update(transform_errors_table).where(transform_errors_table.c.token_id == "tok-1").values(destination="elsewhere"),
                "divergent reasons or destinations",
                id="divergent-destination",
            ),
            pytest.param(
                update(transform_errors_table).values(error_details_json='{"error":"other","reason":"batch_failed"}'),
                "disagrees with the flush node_state",
                id="reason-disagrees-with-the-state",
            ),
            pytest.param(
                transform_errors_table.delete().where(transform_errors_table.c.token_id == "tok-1"),
                "exactly one transform_errors row per member",
                id="a-member-without-its-row",
            ),
            pytest.param(
                routing_events_table.delete(),
                "has flush routing",
                id="named-verdict-without-its-divert",
            ),
        ],
    )
    def test_a_verdict_that_is_not_whole_is_corruption(self, tamper: Executable, match: str) -> None:
        setup = _setup()
        _buffered_batch(setup)
        state_id = _flush_state(setup, "batch-1", "state-1")
        _record_verdict(setup, "batch-1", state_id, destination="quarantine", divert_edge_id=_DIVERT_EDGE)
        with setup.db.write_connection() as conn:
            conn.execute(tamper)

        with pytest.raises(AuditIntegrityError, match=match):
            setup.factory.barrier_restore.list_recorded_aggregation_failures(
                setup.run_id, aggregation_node_id="agg-1", blocked_token_ids=list(_TOKENS)
            )

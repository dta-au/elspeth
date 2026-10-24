"""RowProcessor construction, row lifecycle, aggregation facades, and terminal routing tests."""

from __future__ import annotations

import hashlib
import itertools
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, create_autospec, patch

import pytest

# For node registration
from elspeth.contracts import (
    NodeType,
    TokenInfo,
    TransformProtocol,
    TransformResult,
)
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.declaration_contracts import _attach_contract_name_from_dispatcher
from elspeth.contracts.engine import CommittedAggregationOutputReceipt
from elspeth.contracts.enums import (
    BatchStatus,
    NodeStateStatus,
    TerminalOutcome,
    TerminalPath,
    TriggerType,
)
from elspeth.contracts.errors import (
    AuditIntegrityError,
    BatchPassthroughShapeError,
    FrameworkBugError,
    MaxRetriesExceeded,
    OrchestrationInvariantError,
    SourceGuaranteedFieldsViolation,
)
from elspeth.contracts.scheduler import BarrierTerminalOutcomeSpec, SourceIngestSpec
from elspeth.contracts.types import CoalesceName, CollectorName, NodeID
from elspeth.core.canonical import canonical_json
from elspeth.core.config import AggregationSettings, GateSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.executors.transform import TransformExecutor
from elspeth.engine.orchestrator.ports import TelemetryManagerProtocol
from elspeth.engine.processor import (
    SCHEDULER_MAINTENANCE_INTERVAL,
    BarrierJournalRestoreContext,
    RowProcessor,
    _FlushContext,
)
from elspeth.testing import make_pipeline_row, make_row, make_token_info
from tests.fixtures.audit_hashing import fake_sha256
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import (
    age_barrier_hold,
    landscape_database_now,
    leader_coordination_token,
)
from tests.unit.engine.test_processor import (
    _DYNAMIC_SCHEMA,
    _TEST_LEADER_WORKER_ID,
    _assert_outcome_pair,
    _claim_processor_token,
    _make_contract,
    _make_factory,
    _make_mock_transform,
    _make_processor,
    _make_source_row,
    _persist_blocked_scheduler_work,
    _persist_token_for_scheduler,
)

# =============================================================================
# Constructor: Error Edge Map
# =============================================================================


class TestConstructorErrorEdgeMap:
    """Tests for error edge map construction in __init__."""

    @staticmethod
    def _add_batch_member(factory: RecorderFactory, *, batch_id: str, token_id: str, ordinal: int) -> None:
        """Seed audit membership through the live primitive under leader authority."""
        from elspeth.core.landscape.execution.batches import add_batch_member_guarded
        from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction

        leader = leader_coordination_token(factory, "test-run")
        with fenced_leader_transaction(factory._db.engine, token=leader, window_seconds=60, verb="test_batch_membership") as conn:
            add_batch_member_guarded(
                conn,
                batch_id=batch_id,
                token_id=token_id,
                ordinal=ordinal,
                expected_run_id=leader.run_id,
            )

    def test_scheduler_repository_is_required(self) -> None:
        """RowProcessor must not expose the legacy in-memory drain path."""
        _, factory = _make_factory()

        with pytest.raises(TypeError, match="scheduler"):
            _make_processor(factory, scheduler=None)

    def test_default_scheduler_lease_owner_uses_coordination_token(self) -> None:
        """A processor with a coordination token uses its registered worker identity."""
        _, factory = _make_factory()

        first = _make_processor(factory, scheduler=factory.scheduler)
        second = _make_processor(factory, scheduler=factory.scheduler)

        assert first._scheduler_lease_owner == _TEST_LEADER_WORKER_ID
        assert second._scheduler_lease_owner == _TEST_LEADER_WORKER_ID

    def test_explicit_scheduler_lease_owner_must_match_coordination_token(self) -> None:
        """A leader cannot hold scheduler leases under a second identity."""
        _, factory = _make_factory()

        with pytest.raises(OrchestrationInvariantError, match=r"scheduler_lease_owner.*coordination_token\.worker_id"):
            _make_processor(factory, scheduler=factory.scheduler, scheduler_lease_owner="worker-a")

    def test_navigator_is_constructed_from_traversal_context_factory(self) -> None:
        """RowProcessor should not re-derive DAGNavigator internals at the call site."""
        _, factory = _make_factory()
        nav = SimpleNamespace()
        coalesce_on_success = {CoalesceName("merge"): "out"}
        collector_on_success = {CollectorName("stitch"): "out"}
        sink_names = frozenset({"out", "error"})

        with patch("elspeth.engine.processor.DAGNavigator.from_traversal_context", return_value=nav) as from_traversal:
            processor = _make_processor(
                factory,
                scheduler=factory.scheduler,
                coalesce_on_success_map=coalesce_on_success,
                collector_on_success_map=collector_on_success,
                sink_names=sink_names,
            )

        assert processor._nav is nav
        from_traversal.assert_called_once()
        assert from_traversal.call_args.args == (processor._traversal,)
        assert from_traversal.call_args.kwargs == {
            "coalesce_on_success_map": coalesce_on_success,
            "collector_on_success_map": collector_on_success,
            "sink_names": sink_names,
        }

    def test_fresh_row_drains_throttle_empty_scheduler_maintenance(self) -> None:
        """Fresh source-row drains should not run empty recovery sweeps per row."""
        _, factory = _make_factory()
        processor = _make_processor(factory, scheduler=factory.scheduler)
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with (
            # Slice 3 re-pin (token-bound fixture): a coordination-token-bound
            # processor ingests rows via the fenced composed verb (ADR-030
            # §C.4 row 9), not the unfenced enqueue_ready_claimed arm.
            patch.object(
                factory.scheduler, "ingest_row_with_initial_claim", wraps=factory.scheduler.ingest_row_with_initial_claim
            ) as fenced_ingest,
            patch.object(factory.scheduler, "claim_ready", wraps=factory.scheduler.claim_ready) as claim_ready,
            patch.object(factory.scheduler, "recover_expired_leases", wraps=factory.scheduler.recover_expired_leases) as recover_expired,
        ):
            for idx in range(SCHEDULER_MAINTENANCE_INTERVAL - 1):
                processor.process_row(
                    row_index=idx,
                    source_row=_make_source_row({"value": idx}),
                    transforms=[],
                    ctx=ctx,
                    source_row_index=idx,
                    ingest_sequence=idx,
                )

            assert recover_expired.call_count == 0

            processor.process_row(
                row_index=SCHEDULER_MAINTENANCE_INTERVAL - 1,
                source_row=_make_source_row({"value": SCHEDULER_MAINTENANCE_INTERVAL - 1}),
                transforms=[],
                ctx=ctx,
                source_row_index=SCHEDULER_MAINTENANCE_INTERVAL - 1,
                ingest_sequence=SCHEDULER_MAINTENANCE_INTERVAL - 1,
            )

        assert recover_expired.call_count == 1
        assert fenced_ingest.call_count == SCHEDULER_MAINTENANCE_INTERVAL
        assert claim_ready.call_count == 0

    def test_lease_recovered_work_offsets_transform_attempt_identity(self) -> None:
        """Recovered scheduler attempts must not replay node state attempt 0."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        transform = _make_mock_transform()
        token = make_token_info(row_id="row-1", token_id="token-1", data={"value": 1})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        executor = create_autospec(TransformExecutor, instance=True)
        executor.execute_transform.return_value = (
            TransformResult.success(make_pipeline_row({"value": 1}), success_reason={"action": "test"}),
            token,
            None,
        )
        processor._transform_executor = executor

        processor._execute_transform_with_retry(transform, token, ctx, attempt_offset=1)

        assert executor.execute_transform.call_args.kwargs["attempt"] == 1

    def test_extracts_error_edges_from_edge_map(self) -> None:
        """Error edges (labels like __error_0__) are extracted into error_edge_ids."""
        _, factory = _make_factory()
        edge_map = {
            (NodeID("t1"), "continue"): "edge-1",
            (NodeID("t1"), "__error_0__"): "error-edge-1",
            (NodeID("t2"), "continue"): "edge-2",
            (NodeID("t2"), "__error_1__"): "error-edge-2",
        }
        processor = _make_processor(factory, edge_map=edge_map)
        assert processor._error_edge_ids == {
            NodeID("t1"): "error-edge-1",
            NodeID("t2"): "error-edge-2",
        }

    def test_empty_edge_map_produces_no_error_edges(self) -> None:
        """No edge_map means no error edges."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        assert processor._error_edge_ids == {}

    def test_non_error_labels_ignored(self) -> None:
        """Only __error_N__ labels are extracted; other labels are ignored."""
        _, factory = _make_factory()
        edge_map = {
            (NodeID("t1"), "continue"): "edge-1",
            (NodeID("t1"), "route_to_sink"): "edge-2",
            (NodeID("t1"), "fork_path_a"): "edge-3",
        }
        processor = _make_processor(factory, edge_map=edge_map)
        assert processor._error_edge_ids == {}

    def test_resume_restores_aggregation_buffers_from_blocked_rows(self) -> None:
        """F1 restore inversion: resume rebuilds aggregation buffers FROM journal BLOCKED rows.

        Seeds the journal (2 BLOCKED rows under barrier_key "agg-1", stamped
        via the production claim->mark_blocked path) and the audit trail
        (batch + members, BUFFERED token_outcomes, node_states at attempt 0),
        then builds a resuming processor and asserts the buffers, batch id,
        counters, trigger latch and attempt offsets all derive correctly —
        with NO new journal rows created (the BLOCKED rows are reused as-is).
        """
        from sqlalchemy import func, select

        from elspeth.contracts.barrier_scalars import AggregationNodeScalars, BarrierScalars
        from elspeth.core.landscape.schema import token_work_items_table

        db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        factory.data_flow.register_node(
            plugin_name="agg-transform",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id=str(agg_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        batch = factory.execution.create_batch(
            aggregation_node_id=str(agg_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        tokens: list[TokenInfo] = []
        for ordinal, token_id in enumerate(["t1", "t2"]):
            payload = make_row({"value": ordinal})
            token = TokenInfo(row_id=f"row-{ordinal}", token_id=token_id, row_data=payload)
            _persist_token_for_scheduler(factory, token, ingest_sequence=ordinal)
            tokens.append(token)
            # Audit: membership + BUFFERED outcome + a node_state at attempt 0.
            self._add_batch_member(factory, batch_id=batch.batch_id, token_id=token_id, ordinal=ordinal)
            factory.data_flow.record_token_outcome_leader(
                ref=TokenRef(token_id=token_id, run_id="test-run"),
                outcome=None,
                path=TerminalPath.BUFFERED,
                batch_id=batch.batch_id,
                coordination_token=leader_coordination_token(factory, "test-run"),
            )
            factory.execution.begin_node_state(
                token_id=token_id,
                node_id=str(agg_node),
                step_index=1,
                input_data=payload.to_dict(),
                attempt=0,
                member_token=leader_coordination_token(factory, "test-run").membership,
            )
            # Journal: production-shaped BLOCKED row (mark_blocked stamps
            # barrier_blocked_at; the old ensure_blocked path did not).
            factory.scheduler.enqueue_ready(
                token_id=token_id,
                row_id=token.row_id,
                node_id=str(agg_node),
                step_index=1,
                ingest_sequence=ordinal,
                row_payload_json=factory.scheduler.serialize_row_payload(payload),
                member_token=leader_coordination_token(factory, "test-run").membership,
            )
            claimed = factory.scheduler.claim_ready(
                lease_owner=_TEST_LEADER_WORKER_ID,
                lease_seconds=60,
                member_token=leader_coordination_token(factory, "test-run").membership,
            )
            assert claimed is not None and claimed.token_id == token_id
            factory.scheduler.mark_blocked(
                work_item_id=claimed.work_item_id,
                row_payload_json=claimed.row_payload_json,
                queue_key=None,
                barrier_key=str(agg_node),
                expected_lease_owner=_TEST_LEADER_WORKER_ID,
                member_token=leader_coordination_token(factory, "test-run").membership,
            )
            # The hold is two database seconds old at restore (ADR-047): the
            # restored trigger latch's elapsed age must cover the 1.5 s
            # count_fire_offset the checkpoint carries.
            age_barrier_hold(db.engine, claimed.work_item_id, seconds_ago=2.0)

        with db.connection() as conn:
            rows_before = conn.execute(select(func.count()).select_from(token_work_items_table)).scalar_one()

        # The resuming processor's own clock sits in 2023 (ADR-047 control):
        # the restored latch's age must come from the database, or the
        # drifted clock would report a negative age and refuse the 1.5 s
        # count_fire_offset the checkpoint carries.
        processor = _make_processor(
            factory,
            aggregation_settings={
                agg_node: AggregationSettings(
                    name="test-agg",
                    plugin="test-plugin",
                    input="agg_in",
                    on_error="discard",
                    trigger={"count": 2},
                ),
            },
            clock=MockClock(start=1_700_000_000.0),
            barrier_restore=BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-resume-1",
                barrier_scalars=BarrierScalars(
                    aggregation={str(agg_node): AggregationNodeScalars(count_fire_offset=1.5, condition_fire_offset=None)},
                    coalesce={},
                ),
                batch_id_remap={},
            ),
        )

        node = processor._aggregation_executor._nodes[agg_node]
        assert [t.token_id for t in node.tokens] == ["t1", "t2"]
        assert node.batch_id == batch.batch_id
        assert [t.row_data.to_dict() for t in node.tokens] == [{"value": 0}, {"value": 1}]
        assert node.accepted_count_total == 2
        assert node.completed_flush_count == 0
        assert all(t.resume_attempt_offset == 1 for t in node.tokens)  # max_attempt(0)+1
        assert all(t.resume_checkpoint_id == "ckpt-resume-1" for t in node.tokens)
        # Checkpoint scalars (trigger latch) round-trip through the restore.
        assert node.trigger.get_count_fire_offset() == pytest.approx(1.5)
        # NO new journal rows: the BLOCKED rows are reused as-is.
        with db.connection() as conn:
            rows_after = conn.execute(select(func.count()).select_from(token_work_items_table)).scalar_one()
        assert rows_after == rows_before

    def test_resume_restore_raises_on_orphan_barrier_key(self) -> None:
        """A BLOCKED row whose barrier_key matches no coalesce or aggregation refuses resume."""
        _, factory = _make_factory()
        payload = make_row({"value": 7})
        token = TokenInfo(row_id="row-ghost", token_id="tok-ghost", row_data=payload)
        _persist_token_for_scheduler(factory, token, ingest_sequence=0)
        ghost_item = factory.scheduler.enqueue_ready_claimed(
            token_id="tok-ghost",
            row_id="row-ghost",
            node_id=None,
            step_index=0,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(payload),
            lease_owner=_TEST_LEADER_WORKER_ID,
            lease_seconds=60,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        factory.scheduler.mark_blocked(
            work_item_id=ghost_item.work_item_id,
            row_payload_json=ghost_item.row_payload_json,
            queue_key=None,
            barrier_key="ghost-barrier",
            expected_lease_owner=_TEST_LEADER_WORKER_ID,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        with pytest.raises(AuditIntegrityError, match="orphan barrier_key 'ghost-barrier'"):
            _make_processor(
                factory,
                barrier_restore=BarrierJournalRestoreContext(
                    resume_checkpoint_id="ckpt-resume-1",
                    barrier_scalars=None,
                    batch_id_remap={},
                ),
            )

    def test_resume_restore_ignores_adr028_queue_hold_rows(self) -> None:
        """An ADR-028 queue-hold (BLOCKED, queue_key set, barrier_key NULL) is not a barrier member.

        The queue resume path is untouched by F1: the row must be neither
        restored into a barrier buffer nor counted anywhere, and must stay
        BLOCKED on its queue_key.
        """
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        payload = make_row({"value": 9})
        token = TokenInfo(row_id="row-q", token_id="tok-q", row_data=payload)
        _persist_token_for_scheduler(factory, token, ingest_sequence=0)
        factory.scheduler.enqueue_ready(
            token_id="tok-q",
            row_id="row-q",
            node_id=None,
            step_index=0,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        claimed = factory.scheduler.claim_ready(
            lease_owner=_TEST_LEADER_WORKER_ID,
            lease_seconds=60,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        assert claimed is not None
        factory.scheduler.mark_blocked(
            work_item_id=claimed.work_item_id,
            row_payload_json=claimed.row_payload_json,
            queue_key="queue-1",
            barrier_key=None,
            expected_lease_owner=_TEST_LEADER_WORKER_ID,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        processor = _make_processor(
            factory,
            aggregation_settings={
                agg_node: AggregationSettings(
                    name="test-agg",
                    plugin="test-plugin",
                    input="agg_in",
                    on_error="discard",
                    trigger={"count": 3},
                ),
            },
            barrier_restore=BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-resume-1",
                barrier_scalars=None,
                batch_id_remap={},
            ),
        )

        node = processor._aggregation_executor._nodes[agg_node]
        assert node.tokens == []
        assert node.batch_id is None
        assert node.accepted_count_total == 0
        assert node.completed_flush_count == 0
        with db.connection() as conn:
            status, queue_key, barrier_key = conn.execute(
                select(
                    token_work_items_table.c.status,
                    token_work_items_table.c.queue_key,
                    token_work_items_table.c.barrier_key,
                ).where(token_work_items_table.c.token_id == "tok-q")
            ).one()
        assert (status, queue_key, barrier_key) == ("blocked", "queue-1", None)

    # -- shared seeding for the batch_id-derivation arms ---------------------

    @staticmethod
    def _agg_settings(agg_node: NodeID) -> dict[NodeID, AggregationSettings]:
        return {
            agg_node: AggregationSettings(
                name="test-agg",
                plugin="test-plugin",
                input="agg_in",
                on_error="discard",
                trigger={"count": 3},
            ),
        }

    @staticmethod
    def _restore_ctx(batch_id_remap: dict[str, str] | None = None) -> BarrierJournalRestoreContext:
        return BarrierJournalRestoreContext(
            resume_checkpoint_id="ckpt-resume-1",
            barrier_scalars=None,
            batch_id_remap=batch_id_remap or {},
        )

    @staticmethod
    def _register_aggregation_node(factory: RecorderFactory, agg_node: NodeID) -> None:
        factory.data_flow.register_node(
            plugin_name="agg-transform",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id=str(agg_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

    @staticmethod
    def _seed_blocked_agg_row(factory: RecorderFactory, *, token_id: str, ordinal: int, agg_node: NodeID) -> TokenInfo:
        """Token + production-shaped BLOCKED journal row under the agg barrier.

        Goes through enqueue -> claim -> mark_blocked so barrier_blocked_at is
        stamped exactly as the live buffering path stamps it.
        """
        payload = make_row({"value": ordinal})
        token = TokenInfo(row_id=f"row-{ordinal}", token_id=token_id, row_data=payload)
        _persist_token_for_scheduler(factory, token, ingest_sequence=ordinal)
        factory.scheduler.enqueue_ready(
            token_id=token_id,
            row_id=token.row_id,
            node_id=str(agg_node),
            step_index=1,
            ingest_sequence=ordinal,
            row_payload_json=factory.scheduler.serialize_row_payload(payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        claimed = factory.scheduler.claim_ready(
            lease_owner=_TEST_LEADER_WORKER_ID,
            lease_seconds=60,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        assert claimed is not None and claimed.token_id == token_id
        factory.scheduler.mark_blocked(
            work_item_id=claimed.work_item_id,
            row_payload_json=claimed.row_payload_json,
            queue_key=None,
            barrier_key=str(agg_node),
            expected_lease_owner=_TEST_LEADER_WORKER_ID,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        return token

    def _seed_buffered_member(
        self,
        factory: RecorderFactory,
        *,
        token_id: str,
        ordinal: int,
        agg_node: NodeID,
        batch_id: str,
    ) -> TokenInfo:
        """BLOCKED journal row + batch membership + BUFFERED outcome carrying batch_id."""
        token = self._seed_blocked_agg_row(factory, token_id=token_id, ordinal=ordinal, agg_node=agg_node)
        self._add_batch_member(factory, batch_id=batch_id, token_id=token_id, ordinal=ordinal)
        factory.data_flow.record_token_outcome_leader(
            ref=TokenRef(token_id=token_id, run_id="test-run"),
            outcome=None,
            path=TerminalPath.BUFFERED,
            batch_id=batch_id,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        return token

    def test_resume_restore_reads_batch_id_through_incomplete_batch_remap(self) -> None:
        """A flush-interrupting crash: BUFFERED outcomes carry the DEAD batch id.

        handle_incomplete_batches retries the failed batch (members copied) and
        returns the old->retry mapping; the restore must read each group's
        batch_id THROUGH that remap, land on the retry batch, and count
        accepted tokens DISTINCT-ly (the retry copies would double-count).
        """
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        old_batch = factory.execution.create_batch(
            aggregation_node_id=str(agg_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        for ordinal, token_id in enumerate(["t1", "t2"]):
            self._seed_buffered_member(factory, token_id=token_id, ordinal=ordinal, agg_node=agg_node, batch_id=old_batch.batch_id)
        # Crash shape: flush died -> batch FAILED; resume's
        # handle_incomplete_batches creates the retry batch (members COPIED).
        factory.execution.complete_batch(
            old_batch.batch_id,
            BatchStatus.FAILED,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        retry_batch = factory.execution.retry_batch(
            old_batch.batch_id,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        assert retry_batch.batch_id != old_batch.batch_id

        processor = _make_processor(
            factory,
            aggregation_settings=self._agg_settings(agg_node),
            barrier_restore=self._restore_ctx(batch_id_remap={old_batch.batch_id: retry_batch.batch_id}),
        )

        node = processor._aggregation_executor._nodes[agg_node]
        # Headline: the BUFFERED outcomes still say old_batch; the restored
        # in-progress batch is the RETRY batch via the remap.
        assert node.batch_id == retry_batch.batch_id
        assert [t.token_id for t in node.tokens] == ["t1", "t2"]
        # DISTINCT discipline: members exist in BOTH the dead original and the
        # retry copy; a raw count would say 4.
        assert node.accepted_count_total == 2
        assert node.completed_flush_count == 0

    def test_resume_restore_preserves_counters_when_all_flushes_failed(self) -> None:
        """A node whose flushes all FAILED is still counter-only restored.

        Crash right after a failed flush consumed the batch: accepted rows > 0
        but zero COMPLETED batches, empty buffer, no BLOCKED rows, no scalars
        entry. The accepted counter must survive the resume (it drives
        AggregationBatchContext pagination metadata), not silently reset to 0.
        """
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        failed_batch = factory.execution.create_batch(
            aggregation_node_id=str(agg_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        for ordinal, token_id in enumerate(["t1", "t2"]):
            payload = make_row({"value": ordinal})
            token = TokenInfo(row_id=f"row-{ordinal}", token_id=token_id, row_data=payload)
            _persist_token_for_scheduler(factory, token, ingest_sequence=ordinal)
            self._add_batch_member(factory, batch_id=failed_batch.batch_id, token_id=token_id, ordinal=ordinal)
        factory.execution.complete_batch(
            failed_batch.batch_id,
            BatchStatus.FAILED,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        processor = _make_processor(
            factory,
            aggregation_settings=self._agg_settings(agg_node),
            barrier_restore=self._restore_ctx(),
        )

        node = processor._aggregation_executor._nodes[agg_node]
        assert node.tokens == []
        assert node.batch_id is None
        assert node.accepted_count_total == 2  # pagination metadata survives
        assert node.completed_flush_count == 0

    def test_resume_restore_rejects_barrier_group_split_across_batches(self) -> None:
        """Two BLOCKED tokens in one barrier group resolving DIFFERENT batch ids is corruption."""
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        batch_a = factory.execution.create_batch(
            aggregation_node_id=str(agg_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        batch_b = factory.execution.create_batch(
            aggregation_node_id=str(agg_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        self._seed_buffered_member(factory, token_id="t1", ordinal=0, agg_node=agg_node, batch_id=batch_a.batch_id)
        self._seed_buffered_member(factory, token_id="t2", ordinal=1, agg_node=agg_node, batch_id=batch_b.batch_id)

        with pytest.raises(AuditIntegrityError, match="split across batches"):
            _make_processor(
                factory,
                aggregation_settings=self._agg_settings(agg_node),
                barrier_restore=self._restore_ctx(),
            )

    def test_resume_restore_rejects_blocked_row_with_terminal_outcome(self) -> None:
        """A BLOCKED row whose token already flushed (terminal outcome) is journal/audit divergence.

        Flushed-before-crash shape: the flush recorded BUFFERED -> BATCH_CONSUMED
        but died before terminalizing the journal row. Restoring it would
        double-process the token, so the restore refuses.
        """
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        batch = factory.execution.create_batch(
            aggregation_node_id=str(agg_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        self._seed_buffered_member(factory, token_id="t1", ordinal=0, agg_node=agg_node, batch_id=batch.batch_id)
        factory.data_flow.record_token_outcome_leader(
            ref=TokenRef(token_id="t1", run_id="test-run"),
            outcome=TerminalOutcome.TRANSIENT,
            path=TerminalPath.BATCH_CONSUMED,
            batch_id=batch.batch_id,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with pytest.raises(AuditIntegrityError, match="disagree about this token being buffered"):
            _make_processor(
                factory,
                aggregation_settings=self._agg_settings(agg_node),
                barrier_restore=self._restore_ctx(),
            )

    def test_resume_restore_rejects_remapped_batch_without_batches_row(self) -> None:
        """A remap target with no batches row is audit corruption, not a default."""
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        old_batch = factory.execution.create_batch(
            aggregation_node_id=str(agg_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        self._seed_buffered_member(factory, token_id="t1", ordinal=0, agg_node=agg_node, batch_id=old_batch.batch_id)

        with pytest.raises(AuditIntegrityError, match="has no batches row"):
            _make_processor(
                factory,
                aggregation_settings=self._agg_settings(agg_node),
                barrier_restore=self._restore_ctx(batch_id_remap={old_batch.batch_id: "batch-that-does-not-exist"}),
            )

    def test_resume_restore_rejects_batch_owned_by_foreign_aggregation_node(self) -> None:
        """A group's resolved batch must belong to the barrier_key's own aggregation node."""
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        other_node = NodeID("agg-2")
        self._register_aggregation_node(factory, agg_node)
        self._register_aggregation_node(factory, other_node)
        own_batch = factory.execution.create_batch(
            aggregation_node_id=str(agg_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        foreign_batch = factory.execution.create_batch(
            aggregation_node_id=str(other_node),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        self._seed_buffered_member(factory, token_id="t1", ordinal=0, agg_node=agg_node, batch_id=own_batch.batch_id)

        with pytest.raises(AuditIntegrityError, match="belongs to aggregation node"):
            _make_processor(
                factory,
                aggregation_settings=self._agg_settings(agg_node),
                barrier_restore=self._restore_ctx(batch_id_remap={own_batch.batch_id: foreign_batch.batch_id}),
            )

    def _seed_failed_retry_chain(self, factory: RecorderFactory, agg_node: NodeID, *, attempts: int) -> list[str]:
        """Original batch A plus ``attempts - 1`` retries, every one FAILED.

        The shape repeated crashes inside a flush leave: each resume retries
        the dead batch, and the retry dies too. The members' BUFFERED
        outcomes keep A's id throughout.
        """
        leader = leader_coordination_token(factory, "test-run")
        batch = factory.execution.create_batch(aggregation_node_id=str(agg_node), coordination_token=leader)
        for ordinal, token_id in enumerate(["t1", "t2"]):
            self._seed_buffered_member(factory, token_id=token_id, ordinal=ordinal, agg_node=agg_node, batch_id=batch.batch_id)
        chain = [batch.batch_id]
        factory.execution.complete_batch(batch.batch_id, BatchStatus.FAILED, coordination_token=leader)
        for _ in range(attempts - 1):
            retry = factory.execution.retry_batch(chain[-1], coordination_token=leader)
            factory.execution.complete_batch(retry.batch_id, BatchStatus.FAILED, coordination_token=leader)
            chain.append(retry.batch_id)
        return chain

    @pytest.mark.parametrize("attempts", [2, 3])
    def test_resume_restore_follows_the_retry_chain_to_its_live_end(self, attempts: int) -> None:
        """Repeated crashes: the remap is a CHAIN, and the restore lands on its end.

        ``handle_incomplete_batches`` (the real builder) maps every FAILED
        batch to its retry: A -> B, B -> C (-> D). BUFFERED outcomes still say
        A. One lookup would land on the dead B and the flush would die on the
        immutable-terminal transition with every member left BLOCKED.
        """
        from elspeth.engine.orchestrator.resume import handle_incomplete_batches

        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        chain = self._seed_failed_retry_chain(factory, agg_node, attempts=attempts)

        remap = handle_incomplete_batches(factory.execution, coordination_token=leader_coordination_token(factory, "test-run"))
        live = remap[chain[-1]]
        assert remap == {**dict(itertools.pairwise(chain)), chain[-1]: live}

        processor = _make_processor(
            factory,
            aggregation_settings=self._agg_settings(agg_node),
            barrier_restore=self._restore_ctx(batch_id_remap=remap),
        )

        node = processor._aggregation_executor._nodes[agg_node]
        assert node.batch_id == live
        assert factory.execution.get_batch(live).status is BatchStatus.DRAFT
        assert [t.token_id for t in node.tokens] == ["t1", "t2"]
        assert node.accepted_count_total == 2

    @pytest.mark.parametrize("tip_status", [BatchStatus.FAILED, BatchStatus.COMPLETED], ids=["failed", "completed"])
    def test_resume_restore_rejects_a_retry_chain_ending_at_a_terminal_batch(self, tip_status: BatchStatus) -> None:
        """BLOCKED rows whose chain ends at a finished attempt have nowhere to flush.

        The refusal happens at restore and names the whole chain, instead of
        the flush dying later on the immutable-terminal transition. Both
        terminal statuses are refused: without the COMPLETED arm the restore
        would silently adopt the still-BLOCKED members into a batch that has
        already produced its output.
        """
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        chain = self._seed_failed_retry_chain(factory, agg_node, attempts=2)
        leader = leader_coordination_token(factory, "test-run")
        tip = factory.execution.retry_batch(chain[-1], coordination_token=leader)
        factory.execution.complete_batch(tip.batch_id, tip_status, coordination_token=leader)
        chain.append(tip.batch_id)
        remap = dict(itertools.pairwise(chain))

        with pytest.raises(AuditIntegrityError, match=f"terminal status '{tip_status.value}'") as excinfo:
            _make_processor(
                factory,
                aggregation_settings=self._agg_settings(agg_node),
                barrier_restore=self._restore_ctx(batch_id_remap=remap),
            )
        assert " -> ".join(chain) in str(excinfo.value)

    def test_resume_restore_rejects_a_cyclic_retry_chain(self) -> None:
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        chain = self._seed_failed_retry_chain(factory, agg_node, attempts=2)
        cyclic = {chain[0]: chain[1], chain[1]: chain[0]}

        with pytest.raises(AuditIntegrityError, match="retry chain is cyclic") as excinfo:
            _make_processor(
                factory,
                aggregation_settings=self._agg_settings(agg_node),
                barrier_restore=self._restore_ctx(batch_id_remap=cyclic),
            )
        assert f"cyclic: {chain[0]} -> {chain[1]} -> {chain[0]} — " in str(excinfo.value)

    def test_incomplete_batch_repair_keeps_a_completed_retry_history(self) -> None:
        """Negative control: A FAILED whose retry B COMPLETED is healthy history.

        A stays FAILED for the rest of the run, so every later resume maps it
        to the finished B again. The builder must not refuse that: only a
        BLOCKED row resolving to B is corruption, and the restore decides it.
        """
        from elspeth.engine.orchestrator.resume import handle_incomplete_batches

        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        self._register_aggregation_node(factory, agg_node)
        leader = leader_coordination_token(factory, "test-run")
        original = factory.execution.create_batch(aggregation_node_id=str(agg_node), coordination_token=leader)
        factory.execution.complete_batch(original.batch_id, BatchStatus.FAILED, coordination_token=leader)
        retry = factory.execution.retry_batch(original.batch_id, coordination_token=leader)
        factory.execution.complete_batch(retry.batch_id, BatchStatus.COMPLETED, coordination_token=leader)

        assert handle_incomplete_batches(factory.execution, coordination_token=leader) == {original.batch_id: retry.batch_id}


class TestResolveRetryChain:
    """``resolve_retry_chain``: the one reader of the old->retry edges."""

    def test_a_batch_never_retried_is_a_chain_of_one(self) -> None:
        from elspeth.engine.barrier_coordination import resolve_retry_chain

        assert resolve_retry_chain({"other": "x"}, "a") == ("a",)

    def test_follows_every_hop(self) -> None:
        from elspeth.engine.barrier_coordination import resolve_retry_chain

        assert resolve_retry_chain({"a": "b", "b": "c", "c": "d"}, "a") == ("a", "b", "c", "d")
        assert resolve_retry_chain({"a": "b", "b": "c", "c": "d"}, "b") == ("b", "c", "d")

    @pytest.mark.parametrize(
        ("remap", "named"),
        [
            ({"a": "a"}, "a -> a"),
            ({"a": "b", "b": "c", "c": "a"}, "a -> b -> c -> a"),
            ({"a": "b", "b": "c", "c": "b"}, "a -> b -> c -> b"),
        ],
    )
    def test_a_cycle_is_audit_corruption_naming_the_chain(self, remap: dict[str, str], named: str) -> None:
        from elspeth.engine.barrier_coordination import resolve_retry_chain

        with pytest.raises(AuditIntegrityError, match="retry chain is cyclic") as excinfo:
            resolve_retry_chain(remap, "a")
        assert f"cyclic: {named} — " in str(excinfo.value), "the message names the chain up to the first repeat, no further"


# =============================================================================
# process_row: Linear pipeline (no transforms)
# =============================================================================


class TestProcessRowNoTransforms:
    """Tests for process_row with an empty transform list."""

    @staticmethod
    def _seed_pre_fix_ts02_gap(
        factory: RecorderFactory,
        *,
        token_id: str,
        row_id: str,
        observed_at: datetime,
    ) -> tuple[Any, Any]:
        """Persist the exact legacy row/token + initial scheduler-claim image."""
        from sqlalchemy import delete

        from elspeth.core.landscape.schema import node_states_table

        coordination_token = leader_coordination_token(factory, "test-run")
        source_data = {"value": 42}
        pipeline_row = _make_source_row(source_data).to_pipeline_row()

        _row, _token, work_item = factory.scheduler.ingest_row_with_initial_claim(
            coordination_token=coordination_token,
            source=SourceIngestSpec(
                source_node_id="source-0",
                row_index=0,
                data=source_data,
                source_row_index=0,
                ingest_sequence=0,
                row_id=row_id,
                token_id=token_id,
            ),
            data_flow=factory.data_flow,
            execution=factory.execution,
            node_id=None,
            step_index=1,
            row_payload_json=factory.scheduler.serialize_row_payload(pipeline_row),
            lease_owner=_TEST_LEADER_WORKER_ID,
            lease_seconds=300,
        )
        # The live INGEST verb always includes the source completion. Delete
        # only that witness to manufacture the historical crash image under
        # test; a public callback must not be able to omit this evidence.
        with factory._db.engine.begin() as conn:
            removed = conn.execute(
                delete(node_states_table).where(
                    node_states_table.c.run_id == coordination_token.run_id,
                    node_states_table.c.token_id == token_id,
                    node_states_table.c.node_id == "source-0",
                    node_states_table.c.step_index == 0,
                )
            )
            assert removed.rowcount == 1
        return coordination_token, work_item

    @staticmethod
    def _source_plugin(*, declared_guaranteed_fields: frozenset[str]) -> Any:
        plugin = type("ProcessorSourcePlugin", (), {})()
        plugin.name = "processor-source"
        plugin.node_id = "source-0"
        plugin.declared_guaranteed_fields = declared_guaranteed_fields
        return plugin

    def test_empty_pipeline_returns_completed(self) -> None:
        """Row through empty pipeline gets COMPLETED outcome."""
        _, factory = _make_factory()

        processor = _make_processor(factory)
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        results = processor.process_row(
            row_index=0,
            source_row=source_row,
            transforms=[],
            ctx=ctx,
            source_row_index=0,
            ingest_sequence=0,
        )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].sink_name == "default"
        assert results[0].token.row_data["value"] == 42

    def test_creates_row_and_token_records(self) -> None:
        """process_row creates row record and token in audit trail."""
        _db, factory = _make_factory()

        processor = _make_processor(factory)
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        results = processor.process_row(
            row_index=0,
            source_row=source_row,
            transforms=[],
            ctx=ctx,
            source_row_index=0,
            ingest_sequence=0,
        )

        # Verify token was created (result has a valid token_id)
        token = results[0].token
        assert token.token_id is not None
        assert token.row_id is not None

    def test_records_source_node_state(self) -> None:
        """process_row records a source node_state with COMPLETED status."""
        db, factory = _make_factory()

        processor = _make_processor(factory)
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with (
            patch.object(
                factory.execution, "record_completed_node_state", wraps=factory.execution.record_completed_node_state
            ) as completed_standalone,
            patch.object(
                factory.execution,
                "record_completed_node_state_on",
                wraps=factory.execution.record_completed_node_state_on,
            ) as completed_composed,
            patch.object(factory.execution, "begin_node_state", wraps=factory.execution.begin_node_state) as begin,
            patch.object(factory.execution, "complete_node_state", wraps=factory.execution.complete_node_state) as complete,
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert completed_composed.call_count == 1
        assert completed_standalone.call_count == 0
        assert begin.call_count == 0
        assert complete.call_count == 0

        # Check that source node_state was recorded
        from sqlalchemy import select

        from elspeth.core.landscape.schema import node_states_table

        with db.connection() as conn:
            states = conn.execute(
                select(node_states_table).where(
                    node_states_table.c.run_id == "test-run",
                    node_states_table.c.node_id == "source-0",
                )
            ).fetchall()

        assert len(states) == 1
        [source_state] = states
        assert source_state.node_id == "source-0"
        assert source_state.status == NodeStateStatus.COMPLETED

    def test_fenced_ingest_commits_source_completion_before_return(self) -> None:
        """A hard kill after TS-02 returns cannot strand source audit evidence."""
        db, factory = _make_factory()
        processor = _make_processor(factory)
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        class _SimulatedProcessDeath(BaseException):
            pass

        committed_ingest = processor._ingest_source_row_with_initial_claim

        def crash_after_ingest_commit(**kwargs: Any) -> None:
            committed_ingest(**kwargs)
            raise _SimulatedProcessDeath

        with (
            patch.object(processor, "_ingest_source_row_with_initial_claim", side_effect=crash_after_ingest_commit),
            pytest.raises(_SimulatedProcessDeath),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        from sqlalchemy import func, select

        from elspeth.core.landscape.schema import node_states_table, rows_table, token_work_items_table, tokens_table

        with db.connection() as conn:
            assert conn.execute(select(func.count()).select_from(rows_table)).scalar_one() == 1
            assert conn.execute(select(func.count()).select_from(tokens_table)).scalar_one() == 1
            assert conn.execute(select(func.count()).select_from(token_work_items_table)).scalar_one() == 1
            source_states = conn.execute(
                select(node_states_table).where(
                    node_states_table.c.run_id == "test-run",
                    node_states_table.c.node_id == "source-0",
                )
            ).fetchall()

        assert [(state.status, state.attempt) for state in source_states] == [(NodeStateStatus.COMPLETED.value, 0)], (
            "the fenced ingest commit must include the source COMPLETED witness"
        )

    def test_fenced_ingest_rolls_source_completion_back_with_scheduler_failure(self) -> None:
        """A failure after the source-state insert rolls the whole ingress back."""
        db, factory = _make_factory()
        processor = _make_processor(factory)
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with (
            patch.object(factory.scheduler.queue, "enqueue_ready_claimed_on", side_effect=RuntimeError("injected TS-02 failure")),
            pytest.raises(RuntimeError, match="injected TS-02 failure"),
        ):
            processor.process_row(
                row_index=0,
                source_row=_make_source_row(),
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        from sqlalchemy import func, select

        from elspeth.core.landscape.schema import node_states_table, rows_table, token_work_items_table, tokens_table

        with db.connection() as conn:
            assert conn.execute(select(func.count()).select_from(rows_table)).scalar_one() == 0
            assert conn.execute(select(func.count()).select_from(tokens_table)).scalar_one() == 0
            assert conn.execute(select(func.count()).select_from(node_states_table)).scalar_one() == 0
            assert conn.execute(select(func.count()).select_from(token_work_items_table)).scalar_one() == 0

    def test_source_completion_reconciliation_rejects_conflicting_state(self) -> None:
        """A fully witnessed TS-02 image never overwrites conflicting evidence."""
        _db, factory = _make_factory()
        coordination_token, _work_item = self._seed_pre_fix_ts02_gap(
            factory,
            token_id="token-conflicting-source-state",
            row_id="row-conflicting-source-state",
            observed_at=landscape_database_now(factory._db.engine),
        )
        factory.execution.record_completed_node_state(
            token_id="token-conflicting-source-state",
            node_id="source-0",
            step_index=0,
            input_data={"value": 999},
            output_data={"value": 999},
            duration_ms=0,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with pytest.raises(AuditIntegrityError, match="conflicting audit evidence"):
            factory.execution.reconcile_source_completions_from_scheduler(
                coordination_token=coordination_token,
            )

    def test_source_completion_reconciliation_rejects_duplicate_claim_witness(self) -> None:
        """Duplicate scheduler claims are ambiguous and must not synthesize evidence."""
        db, factory = _make_factory()
        observed_at = datetime.now(UTC)
        token_id = "token-duplicate-claim"
        coordination_token, work_item = self._seed_pre_fix_ts02_gap(
            factory,
            token_id=token_id,
            row_id="row-duplicate-claim",
            observed_at=observed_at,
        )

        from sqlalchemy import select

        from elspeth.core.landscape.schema import node_states_table, scheduler_events_table

        with db.write_connection() as conn:
            claim = (
                conn.execute(
                    select(scheduler_events_table).where(
                        scheduler_events_table.c.run_id == "test-run",
                        scheduler_events_table.c.token_id == token_id,
                        scheduler_events_table.c.event_type == "claim_ready",
                    )
                )
                .mappings()
                .one()
            )
            # A duplicate witness is the same transition recorded twice: same
            # content id, a fresh epoch-38 seq assigned by the database.
            values = {column: value for column, value in claim.items() if column != "seq"}
            conn.execute(scheduler_events_table.insert().values(**values))

        with pytest.raises(AuditIntegrityError, match="exactly two scheduler events"):
            factory.execution.reconcile_source_completions_from_scheduler(
                coordination_token=coordination_token,
            )
        with db.connection() as conn:
            assert conn.execute(select(node_states_table).where(node_states_table.c.token_id == token_id)).all() == []
        assert work_item.attempt == 1

    def test_source_completion_reconciliation_rejects_mismatched_work_item_witness(self) -> None:
        """Token-level history cannot stand in for the current work-item witness."""
        db, factory = _make_factory()
        observed_at = datetime.now(UTC)
        token_id = "token-mismatched-work-item"
        coordination_token, _work_item = self._seed_pre_fix_ts02_gap(
            factory,
            token_id=token_id,
            row_id="row-mismatched-work-item",
            observed_at=observed_at,
        )

        from sqlalchemy import select, update

        from elspeth.core.landscape.schema import node_states_table, scheduler_events_table

        with db.write_connection() as conn:
            conn.execute(
                update(scheduler_events_table)
                .where(
                    scheduler_events_table.c.run_id == "test-run",
                    scheduler_events_table.c.token_id == token_id,
                    scheduler_events_table.c.event_type == "claim_ready",
                )
                .values(work_item_id="different-work-item")
            )

        with pytest.raises(AuditIntegrityError, match="exactly two scheduler events"):
            factory.execution.reconcile_source_completions_from_scheduler(
                coordination_token=coordination_token,
            )
        with db.connection() as conn:
            assert conn.execute(select(node_states_table).where(node_states_table.c.token_id == token_id)).all() == []

    def test_source_completion_reconciliation_rejects_later_attempt(self) -> None:
        """Historical attempt-1 events cannot authorize repair at attempt 2."""
        db, factory = _make_factory()
        observed_at = datetime.now(UTC)
        token_id = "token-later-attempt"
        coordination_token, _work_item = self._seed_pre_fix_ts02_gap(
            factory,
            token_id=token_id,
            row_id="row-later-attempt",
            observed_at=observed_at,
        )

        from sqlalchemy import select, update

        from elspeth.core.landscape.schema import node_states_table, token_work_items_table

        with db.write_connection() as conn:
            conn.execute(update(token_work_items_table).where(token_work_items_table.c.token_id == token_id).values(attempt=2))

        with pytest.raises(AuditIntegrityError, match="requires attempt=1"):
            factory.execution.reconcile_source_completions_from_scheduler(
                coordination_token=coordination_token,
            )
        with db.connection() as conn:
            assert conn.execute(select(node_states_table).where(node_states_table.c.token_id == token_id)).all() == []

    def test_source_completion_reconciliation_rejects_malformed_existing_state(self) -> None:
        """Unreadable COMPLETED evidence is a conflict, never an idempotent success."""
        db, factory = _make_factory()
        observed_at = datetime.now(UTC)
        token_id = "token-malformed-source-state"
        coordination_token, _work_item = self._seed_pre_fix_ts02_gap(
            factory,
            token_id=token_id,
            row_id="row-malformed-source-state",
            observed_at=observed_at,
        )
        factory.execution.record_completed_node_state(
            token_id=token_id,
            node_id="source-0",
            step_index=0,
            input_data={"value": 42},
            output_data={"value": 42},
            duration_ms=0,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        from sqlalchemy import select, update

        from elspeth.core.landscape.schema import node_states_table

        with db.write_connection() as conn:
            conn.execute(update(node_states_table).where(node_states_table.c.token_id == token_id).values(completed_at=None))

        with pytest.raises(AuditIntegrityError, match="completed_at"):
            factory.execution.reconcile_source_completions_from_scheduler(
                coordination_token=coordination_token,
            )
        with db.connection() as conn:
            assert conn.execute(select(node_states_table).where(node_states_table.c.token_id == token_id)).all() != []

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("context_before_json", "{}"),
            ("context_after_json", "{}"),
            ("success_reason_json", '{"action":"pass"}'),
            ("resume_checkpoint_id", "checkpoint-impossible-for-source"),
        ],
    )
    def test_source_completion_reconciliation_rejects_source_impossible_metadata(self, field: str, value: str) -> None:
        """A source witness cannot carry transform or resume provenance."""
        db, factory = _make_factory()
        observed_at = datetime.now(UTC)
        token_id = f"token-source-metadata-{field}"
        coordination_token, _work_item = self._seed_pre_fix_ts02_gap(
            factory,
            token_id=token_id,
            row_id=f"row-source-metadata-{field}",
            observed_at=observed_at,
        )
        factory.execution.record_completed_node_state(
            token_id=token_id,
            node_id="source-0",
            step_index=0,
            input_data={"value": 42},
            output_data={"value": 42},
            duration_ms=0,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        from sqlalchemy import update

        from elspeth.core.landscape.schema import node_states_table

        with db.write_connection() as conn:
            conn.execute(update(node_states_table).where(node_states_table.c.token_id == token_id).values({field: value}))

        with pytest.raises(AuditIntegrityError, match="conflicting audit evidence"):
            factory.execution.reconcile_source_completions_from_scheduler(
                coordination_token=coordination_token,
            )

    def test_source_boundary_violation_records_failed_outcome_and_failed_source_state(self) -> None:
        """Tier-1 source boundary failures must still leave terminal audit evidence."""
        db, factory = _make_factory()
        processor = _make_processor(
            factory,
            source_plugin=self._source_plugin(declared_guaranteed_fields=frozenset({"customer_id"})),
        )
        source_row = _make_source_row({"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def _raise_source_boundary(*args: Any, **kwargs: Any) -> None:
            violation = SourceGuaranteedFieldsViolation(
                plugin="processor-source",
                node_id="source-0",
                run_id="test-run",
                row_id="row_0",
                token_id="token_0",
                payload={
                    "declared": ["customer_id"],
                    "runtime_observed": [],
                    "missing": ["customer_id"],
                },
                message="source boundary failed",
            )
            _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
            raise violation

        with (
            patch("elspeth.engine.processor.run_boundary_checks", side_effect=_raise_source_boundary),
            pytest.raises(SourceGuaranteedFieldsViolation),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        from sqlalchemy import select

        from elspeth.core.landscape.schema import node_states_table, token_outcomes_table

        with db.connection() as conn:
            states = conn.execute(
                select(node_states_table).where(
                    node_states_table.c.run_id == "test-run",
                    node_states_table.c.node_id == "source-0",
                )
            ).fetchall()
            outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == "test-run")).fetchall()

        assert len(states) == 1
        [source_state] = states
        assert source_state.node_id == "source-0"
        assert source_state.status == NodeStateStatus.FAILED
        assert len(outcomes) == 1
        _assert_outcome_pair(outcomes[0], TerminalOutcome.FAILURE, TerminalPath.UNROUTED)

    def test_source_boundary_violation_on_secondary_source_uses_secondary_source_identity(self) -> None:
        """Multi-source boundary failures must be attributed to the row's source root."""
        db, factory = _make_factory()
        factory.data_flow.register_node(
            plugin_name="secondary-source",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            node_id="source-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        processor = _make_processor(
            factory,
            source_plugin=self._source_plugin(declared_guaranteed_fields=frozenset({"customer_id"})),
        )
        source_row = _make_source_row({"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def _raise_source_boundary(*args: Any, **kwargs: Any) -> None:
            violation = SourceGuaranteedFieldsViolation(
                plugin="secondary-source",
                node_id="source-1",
                run_id="test-run",
                row_id="row_0",
                token_id="token_0",
                payload={
                    "declared": ["customer_id"],
                    "runtime_observed": [],
                    "missing": ["customer_id"],
                },
                message="secondary source boundary failed",
            )
            _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
            raise violation

        with (
            patch("elspeth.engine.processor.run_boundary_checks", side_effect=_raise_source_boundary),
            patch("elspeth.engine.processor.best_effort", return_value=nullcontext()) as best_effort_context,
            pytest.raises(SourceGuaranteedFieldsViolation, match="secondary source boundary failed"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_node_id=NodeID("source-1"),
                source_row_index=0,
                ingest_sequence=0,
            )

        from sqlalchemy import select

        from elspeth.core.landscape.schema import node_states_table, rows_table, token_outcomes_table, tokens_table

        with db.connection() as conn:
            failed_states = conn.execute(
                select(node_states_table).where(
                    node_states_table.c.run_id == "test-run",
                    node_states_table.c.status == NodeStateStatus.FAILED,
                )
            ).fetchall()
            outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == "test-run")).fetchall()
            row_source_node_id = conn.execute(
                select(rows_table.c.source_node_id)
                .join(tokens_table, rows_table.c.row_id == tokens_table.c.row_id)
                .where(tokens_table.c.run_id == "test-run")
            ).scalar_one()

        assert row_source_node_id == "source-1"
        assert len(failed_states) == 1
        [source_state] = failed_states
        assert source_state.node_id == "source-1"
        assert len(outcomes) == 1
        expected_hash = hashlib.sha256(b"SourceGuaranteedFieldsViolation:source-1").hexdigest()[:16]
        assert outcomes[0].error_hash == expected_hash
        best_effort_context.assert_called_once()
        assert best_effort_context.call_args.kwargs["source_node_id"] == NodeID("source-1")

    def test_source_boundary_landscape_record_error_raises_audit_integrity_error(self) -> None:
        """Narrow recorder failures outrank the original boundary violation."""
        _db, factory = _make_factory()
        processor = _make_processor(
            factory,
            source_plugin=self._source_plugin(declared_guaranteed_fields=frozenset({"customer_id"})),
        )
        source_row = _make_source_row({"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def _raise_source_boundary(*args: Any, **kwargs: Any) -> None:
            violation = SourceGuaranteedFieldsViolation(
                plugin="processor-source",
                node_id="source-0",
                run_id="test-run",
                row_id="row_0",
                token_id="token_0",
                payload={
                    "declared": ["customer_id"],
                    "runtime_observed": [],
                    "missing": ["customer_id"],
                },
                message="source boundary failed",
            )
            _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
            raise violation

        with (
            patch("elspeth.engine.processor.run_boundary_checks", side_effect=_raise_source_boundary),
            patch.object(factory.data_flow, "record_token_outcome_leader", side_effect=LandscapeRecordError("token outcome DB down")),
            pytest.raises(
                AuditIntegrityError,
                match=r"Failed to record SourceGuaranteedFieldsViolation FAILED outcome for token .* on source boundary",
            ) as exc_info,
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert isinstance(exc_info.value.__cause__, LandscapeRecordError)
        assert "Original violation: source boundary failed" in str(exc_info.value)

    def test_source_boundary_value_error_from_token_outcome_propagates_plainly(self) -> None:
        """Programmer bugs in token-outcome recording must not be reclassified."""
        _db, factory = _make_factory()
        processor = _make_processor(
            factory,
            source_plugin=self._source_plugin(declared_guaranteed_fields=frozenset({"customer_id"})),
        )
        source_row = _make_source_row({"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def _raise_source_boundary(*args: Any, **kwargs: Any) -> None:
            violation = SourceGuaranteedFieldsViolation(
                plugin="processor-source",
                node_id="source-0",
                run_id="test-run",
                row_id="row_0",
                token_id="token_0",
                payload={
                    "declared": ["customer_id"],
                    "runtime_observed": [],
                    "missing": ["customer_id"],
                },
                message="source boundary failed",
            )
            _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
            raise violation

        with (
            patch("elspeth.engine.processor.run_boundary_checks", side_effect=_raise_source_boundary),
            patch.object(factory.data_flow, "record_token_outcome_leader", side_effect=ValueError("recorder bug")),
            pytest.raises(ValueError, match="recorder bug"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

    def test_source_boundary_state_landscape_record_error_raises_audit_integrity_error(self) -> None:
        """FAILED source-state recorder failures outrank the original violation."""
        _db, factory = _make_factory()
        processor = _make_processor(
            factory,
            source_plugin=self._source_plugin(declared_guaranteed_fields=frozenset({"customer_id"})),
        )
        source_row = _make_source_row({"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def _raise_source_boundary(*args: Any, **kwargs: Any) -> None:
            violation = SourceGuaranteedFieldsViolation(
                plugin="processor-source",
                node_id="source-0",
                run_id="test-run",
                row_id="row_0",
                token_id="token_0",
                payload={
                    "declared": ["customer_id"],
                    "runtime_observed": [],
                    "missing": ["customer_id"],
                },
                message="source boundary failed",
            )
            _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
            raise violation

        with (
            patch("elspeth.engine.processor.run_boundary_checks", side_effect=_raise_source_boundary),
            patch.object(factory.execution, "complete_node_state", side_effect=LandscapeRecordError("source state DB down")),
            pytest.raises(
                AuditIntegrityError,
                match=r"Failed to record FAILED source node state for token .* on source boundary",
            ) as exc_info,
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert isinstance(exc_info.value.__cause__, LandscapeRecordError)
        assert "Original violation: source boundary failed" in str(exc_info.value)

    def test_source_boundary_runtime_error_from_node_state_recording_propagates_plainly(self) -> None:
        """Programmer bugs in source-state recording must not be reclassified."""
        _db, factory = _make_factory()
        processor = _make_processor(
            factory,
            source_plugin=self._source_plugin(declared_guaranteed_fields=frozenset({"customer_id"})),
        )
        source_row = _make_source_row({"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def _raise_source_boundary(*args: Any, **kwargs: Any) -> None:
            violation = SourceGuaranteedFieldsViolation(
                plugin="processor-source",
                node_id="source-0",
                run_id="test-run",
                row_id="row_0",
                token_id="token_0",
                payload={
                    "declared": ["customer_id"],
                    "runtime_observed": [],
                    "missing": ["customer_id"],
                },
                message="source boundary failed",
            )
            _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
            raise violation

        with (
            patch("elspeth.engine.processor.run_boundary_checks", side_effect=_raise_source_boundary),
            patch.object(factory.execution, "complete_node_state", side_effect=RuntimeError("source state bug")),
            pytest.raises(RuntimeError, match="source state bug"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

    def test_source_boundary_token_completed_telemetry_failure_does_not_interrupt_audit_recording(self) -> None:
        """Best-effort telemetry must not break the FAILED-audit pair on source violations."""
        db, factory = _make_factory()
        processor = _make_processor(
            factory,
            source_plugin=self._source_plugin(declared_guaranteed_fields=frozenset({"customer_id"})),
        )
        source_row = _make_source_row({"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def _raise_source_boundary(*args: Any, **kwargs: Any) -> None:
            violation = SourceGuaranteedFieldsViolation(
                plugin="processor-source",
                node_id="source-0",
                run_id="test-run",
                row_id="row_0",
                token_id="token_0",
                payload={
                    "declared": ["customer_id"],
                    "runtime_observed": [],
                    "missing": ["customer_id"],
                },
                message="source boundary failed",
            )
            _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
            raise violation

        with (
            patch("elspeth.engine.processor.run_boundary_checks", side_effect=_raise_source_boundary),
            patch.object(processor, "_emit_token_completed", side_effect=RuntimeError("telemetry down")),
            pytest.raises(SourceGuaranteedFieldsViolation, match="source boundary failed"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        from sqlalchemy import select

        from elspeth.core.landscape.schema import node_states_table, token_outcomes_table

        with db.connection() as conn:
            states = conn.execute(
                select(node_states_table).where(
                    node_states_table.c.run_id == "test-run",
                    node_states_table.c.node_id == "source-0",
                )
            ).fetchall()
            outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == "test-run")).fetchall()

        assert len(states) == 1
        [source_state] = states
        assert source_state.node_id == "source-0"
        assert source_state.status == NodeStateStatus.FAILED
        assert len(outcomes) == 1
        _assert_outcome_pair(outcomes[0], TerminalOutcome.FAILURE, TerminalPath.UNROUTED)

    def test_source_boundary_framework_bug_records_failed_outcome_and_failed_source_state(self) -> None:
        """Tier-1 framework bugs after token creation must still leave terminal audit evidence."""
        db, factory = _make_factory()
        processor = _make_processor(
            factory,
            source_plugin=self._source_plugin(declared_guaranteed_fields=frozenset({"customer_id"})),
        )
        source_row = _make_source_row({"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with (
            patch(
                "elspeth.engine.processor.run_boundary_checks",
                side_effect=FrameworkBugError("source row crossed boundary without a schema contract"),
            ),
            pytest.raises(FrameworkBugError, match="without a schema contract"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        from sqlalchemy import select

        from elspeth.core.landscape.schema import node_states_table, token_outcomes_table

        with db.connection() as conn:
            states = conn.execute(
                select(node_states_table).where(
                    node_states_table.c.run_id == "test-run",
                    node_states_table.c.node_id == "source-0",
                )
            ).fetchall()
            outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == "test-run")).fetchall()

        assert len(states) == 1
        [source_state] = states
        assert source_state.node_id == "source-0"
        assert source_state.status == NodeStateStatus.FAILED
        assert len(outcomes) == 1
        _assert_outcome_pair(outcomes[0], TerminalOutcome.FAILURE, TerminalPath.UNROUTED)

    def test_batch_flush_token_completed_telemetry_failure_does_not_interrupt_failed_audit_recording(self) -> None:
        """Best-effort telemetry must not interrupt per-token FAILED batch-flush audit writes."""
        _db, factory = _make_factory()
        processor = _make_processor(factory)
        transform = _make_mock_transform(node_id="aggregate-1", name="batch-transform")
        token_a = make_token_info(row_id="row-a", token_id="token-a", data={"value": 1})
        token_b = make_token_info(row_id="row-b", token_id="token-b", data={"value": 2})
        violation = SourceGuaranteedFieldsViolation(
            plugin="batch-transform",
            node_id="aggregate-1",
            run_id="test-run",
            row_id="row-a",
            token_id="token-a",
            payload={
                "declared": ["customer_id"],
                "runtime_observed": [],
                "missing": ["customer_id"],
            },
            message="batch flush failed",
        )
        _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
        fctx = _FlushContext(
            node_id=NodeID("aggregate-1"),
            transform=transform,
            settings=AggregationSettings(
                name="agg",
                plugin="batch-plugin",
                input="source",
                on_error="discard",
                trigger={"count": 2},
            ),
            buffered_tokens=(token_a, token_b),
            batch_id="batch-1",
            expand_parent_token=token_a,
            triggering_token=token_b,
            coalesce_node_id=None,
            coalesce_name=None,
        )

        with (
            patch.object(factory.data_flow, "record_token_outcome_leader") as mock_record_token_outcome,
            patch.object(processor, "_emit_token_completed", side_effect=RuntimeError("telemetry down")),
        ):
            processor._record_flush_violation(fctx, violation)

        assert mock_record_token_outcome.call_count == 2
        recorded_refs = {call.kwargs["ref"].token_id for call in mock_record_token_outcome.call_args_list}
        assert recorded_refs == {"token-a", "token-b"}

    def test_buffered_scheduler_barrier_key_requires_live_hold_stash(self) -> None:
        """A BUFFERED claim result without a live barrier-hold stash is a processor bug.

        Slice 3 re-pin (ADR-030 §E.2): the barrier_key for the drain's
        mark_blocked no longer derives from the (now nonexistent) in-claim
        BUFFERED outcome — the producer stashes the live hold; a missing
        stash entry is refused loudly.
        """
        _db, factory = _make_factory()
        processor = _make_processor(factory)

        with pytest.raises(AuditIntegrityError, match="no live barrier hold stash"):
            processor._live_barrier_hold("token-a")

        arriving = make_token_info(row_id="row-a", token_id="token-a", data={"value": 1})
        processor._record_barrier_arrival(arriving, barrier_key="aggregation_a")
        hold = processor._live_barrier_hold("token-a")
        assert hold.barrier_key == "aggregation_a"
        assert hold.token is arriving

    def test_empty_batch_flush_plans_dropped_outcomes_without_early_audit_writes(self) -> None:
        """Zero-row routing stays pure until the atomic barrier completion."""
        _db, factory = _make_factory()
        telemetry_manager = create_autospec(TelemetryManagerProtocol, instance=True)
        telemetry_manager.handle_event.side_effect = RuntimeError("telemetry down")
        processor = _make_processor(factory, telemetry_manager=telemetry_manager)
        transform = _make_mock_transform(node_id="aggregate-1", name="batch-transform")
        token_a = make_token_info(row_id="row-a", token_id="token-a", data={"value": 1})
        token_b = make_token_info(row_id="row-b", token_id="token-b", data={"value": 2})
        token_c = make_token_info(row_id="row-c", token_id="token-c", data={"value": 3})
        fctx = _FlushContext(
            node_id=NodeID("aggregate-1"),
            transform=transform,
            settings=AggregationSettings(
                name="agg",
                plugin="batch-plugin",
                input="source",
                on_error="discard",
                trigger={"count": 3},
            ),
            buffered_tokens=(token_a, token_b, token_c),
            batch_id="batch-1",
            expand_parent_token=token_a,
            triggering_token=token_c,
            coalesce_node_id=None,
            coalesce_name=None,
        )

        with patch.object(factory.data_flow, "record_token_outcome") as mock_record_token_outcome:
            results, child_items = processor._route_empty_emission_results(fctx)

        mock_record_token_outcome.assert_not_called()
        telemetry_manager.handle_event.assert_not_called()
        assert child_items == []
        assert tuple((result.outcome, result.path) for result in results) == (
            (TerminalOutcome.SUCCESS, TerminalPath.FILTER_DROPPED),
            (TerminalOutcome.SUCCESS, TerminalPath.FILTER_DROPPED),
            (TerminalOutcome.SUCCESS, TerminalPath.FILTER_DROPPED),
        )

    def test_empty_batch_flush_does_not_reach_non_atomic_recorder(self) -> None:
        """The legacy per-token recorder is outside zero-row routing."""
        _db, factory = _make_factory()
        processor = _make_processor(factory)
        transform = _make_mock_transform(node_id="aggregate-1", name="batch-transform")
        token_a = make_token_info(row_id="row-a", token_id="token-a", data={"value": 1})
        token_b = make_token_info(row_id="row-b", token_id="token-b", data={"value": 2})
        fctx = _FlushContext(
            node_id=NodeID("aggregate-1"),
            transform=transform,
            settings=AggregationSettings(
                name="agg",
                plugin="batch-plugin",
                input="source",
                on_error="discard",
                trigger={"count": 2},
            ),
            buffered_tokens=(token_a, token_b),
            batch_id="batch-1",
            expand_parent_token=token_a,
            triggering_token=token_b,
            coalesce_node_id=None,
            coalesce_name=None,
        )

        with patch.object(factory.data_flow, "record_token_outcome", side_effect=LandscapeRecordError("audit DB down")) as recorder:
            results, child_items = processor._route_empty_emission_results(fctx)

        recorder.assert_not_called()
        assert child_items == []
        assert [result.token.token_id for result in results] == ["token-a", "token-b"]

    def test_success_empty_recorder_failure_raises_audit_integrity_error(self) -> None:
        """Typed recorder failures during single-row success_empty terminalization must outrank success."""
        _db, factory = _make_factory()
        processor = _make_processor(factory)
        transform = _make_mock_transform(node_id="filter-1", name="dropper")
        token = make_token_info(row_id="row-drop", token_id="token-drop", data={"value": 1})
        factory.data_flow.register_node(
            coordination_token=leader_coordination_token(factory, "test-run"),
            node_id="filter-1",
            plugin_name="dropper",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            schema_config=_DYNAMIC_SCHEMA,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
            work_item=_claim_processor_token(factory, token, node_id="filter-1", run_id="test-run"),
        )

        with (
            patch.object(
                processor,
                "_execute_transform_with_retry",
                return_value=(TransformResult.success_empty(success_reason={"action": "filtered"}), token, None),
            ),
            patch.object(factory.data_flow, "record_token_outcome", side_effect=LandscapeRecordError("audit DB down")),
            pytest.raises(
                AuditIntegrityError,
                match=r"Failed to record DROPPED_BY_FILTER outcome for token 'token-drop'",
            ) as exc_info,
        ):
            processor._handle_transform_node(
                transform=transform,
                current_token=token,
                ctx=ctx,
                node_id=NodeID("filter-1"),
                child_items=[],
                coalesce_node_id=None,
                coalesce_name=None,
                current_on_success_sink="default",
                attempt_offset=0,
            )

        assert isinstance(exc_info.value.__cause__, LandscapeRecordError)

    def test_batch_flush_non_recorder_recording_bug_propagates_unmodified(self) -> None:
        """Only recorder failures become AuditIntegrityError on batch-flush auto-fail."""
        _db, factory = _make_factory()
        processor = _make_processor(factory)
        transform = _make_mock_transform(node_id="aggregate-1", name="batch-transform")
        token_a = make_token_info(row_id="row-a", token_id="token-a", data={"value": 1})
        token_b = make_token_info(row_id="row-b", token_id="token-b", data={"value": 2})
        violation = SourceGuaranteedFieldsViolation(
            plugin="batch-transform",
            node_id="aggregate-1",
            run_id="test-run",
            row_id="row-a",
            token_id="token-a",
            payload={
                "declared": ["customer_id"],
                "runtime_observed": [],
                "missing": ["customer_id"],
            },
            message="batch flush failed",
        )
        _attach_contract_name_from_dispatcher(violation, "source_guaranteed_fields")
        fctx = _FlushContext(
            node_id=NodeID("aggregate-1"),
            transform=transform,
            settings=AggregationSettings(
                name="agg",
                plugin="batch-plugin",
                input="source",
                on_error="discard",
                trigger={"count": 2},
            ),
            buffered_tokens=(token_a, token_b),
            batch_id="batch-1",
            expand_parent_token=token_a,
            triggering_token=token_b,
            coalesce_node_id=None,
            coalesce_name=None,
        )
        invariant_failure = RuntimeError("wrong-run token ownership")

        with (
            patch.object(factory.data_flow, "record_token_outcome_leader", side_effect=invariant_failure),
            patch.object(processor, "_emit_token_completed") as mock_emit,
            pytest.raises(RuntimeError, match="wrong-run token ownership") as exc_info,
        ):
            processor._record_flush_violation(fctx, violation)

        assert exc_info.value is invariant_failure
        mock_emit.assert_not_called()


# =============================================================================
# process_row: Single transform
# =============================================================================


class TestProcessRowSingleTransform:
    """Tests for process_row with a single transform."""

    def _setup(self, transform: Any) -> tuple[LandscapeDB, RecorderFactory, RowProcessor]:
        db, factory = _make_factory()
        source_node = NodeID("source-0")
        transform_node = NodeID(transform.node_id)

        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, transform_node: 1},
            node_to_next={source_node: transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
        )
        return db, factory, processor

    def test_successful_transform_returns_completed(self) -> None:
        """Row passes through transform → COMPLETED."""
        transform = _make_mock_transform()
        _db, factory, processor = self._setup(transform)
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        output_data = make_row({"value": 10, "enriched": True})
        success_result = TransformResult.success(
            output_data,
            success_reason={"action": "test"},
        )

        # side_effect receives the real token and returns it with the desired result
        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (success_result, token, None)

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=executor_side_effect,
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].sink_name == "default"

    def test_transform_error_with_discard_returns_quarantined(self) -> None:
        """Transform error with on_error='discard' → QUARANTINED."""
        transform = _make_mock_transform(on_error="discard")
        _db, factory, processor = self._setup(transform)
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        error_result = TransformResult.error(
            {"reason": "test_error"},
            retryable=False,
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (error_result, token, "discard")

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=executor_side_effect,
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)

    def test_transform_error_with_named_sink_returns_routed_on_error(self) -> None:
        """Transform error with on_error='errors' → ROUTED_ON_ERROR (DIVERT) to error sink.

        elspeth-5069612f3c: transform on_error path emits the dedicated
        ROUTED_ON_ERROR outcome (rows_routed_failure), distinct from the
        gate route_to_sink MOVE which uses ROUTED (rows_routed_success).
        """
        transform = _make_mock_transform(on_error="errors")
        _db, factory, processor = self._setup(transform)
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        error_result = TransformResult.error(
            {"reason": "test_error"},
            retryable=False,
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (error_result, token, "errors")

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=executor_side_effect,
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.FAILURE, TerminalPath.ON_ERROR_ROUTED)
        assert results[0].sink_name == "errors"

    def test_max_retries_exceeded_returns_failed(self) -> None:
        """MaxRetriesExceeded → FAILED outcome."""
        transform = _make_mock_transform()
        _db, factory, processor = self._setup(transform)
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=MaxRetriesExceeded(3, Exception("boom")),
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.FAILURE, TerminalPath.UNROUTED)
        assert results[0].error is not None


class TestAggregationFailureMatrix:
    """Focused aggregation failure/regression matrix coverage."""

    def _setup_batch_processor(
        self,
        *,
        output_mode: str,
        node_to_next: dict[NodeID, NodeID | None] | None = None,
        transform_on_success: str | None = "agg_sink",
        on_error: str = "discard",
    ) -> tuple[LandscapeDB, RecorderFactory, RowProcessor, Mock, NodeID]:
        """Create a RowProcessor configured for a single batch-aware aggregation node."""
        db, factory = _make_factory()
        source_node = NodeID("source-0")
        agg_node = NodeID("agg-1")

        transform = _make_mock_transform(
            node_id=str(agg_node),
            name="agg-transform",
            is_batch_aware=True,
            on_success=transform_on_success,
        )

        traversal_next = (
            dict(node_to_next)
            if node_to_next is not None
            else {
                source_node: agg_node,
                agg_node: None,
            }
        )

        # Ensure source node has an explicit next mapping.
        traversal_next.setdefault(source_node, agg_node)

        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, agg_node: 1, NodeID("downstream-2"): 2},
            node_to_next=traversal_next,
            node_to_plugin={agg_node: transform},
            aggregation_settings={
                agg_node: AggregationSettings(
                    name="batch_agg",
                    plugin="agg-transform",
                    input="default",
                    on_error=on_error,
                    trigger={"count": 1},
                    output_mode=output_mode,
                ),
            },
        )
        return db, factory, processor, transform, agg_node

    @pytest.mark.parametrize("output_mode", ["passthrough", "transform"])
    def test_flush_failure_with_discard_terminates_every_member_inside_the_barrier(self, output_mode: str) -> None:
        """Operator ruling B3: ``on_error: discard`` at the batch seam matches the
        per-row discard. Every buffered member is (FAILURE, QUARANTINED_AT_SOURCE)
        with an error_hash that binds to the batch reason (B4), and each terminal
        is written INSIDE the complete_barrier transaction that consumes its
        BLOCKED row — no processor-side leader write, no second transaction to
        crash between. A TokenCompleted telemetry failure afterwards does not
        undo or interrupt the durable terminals.
        """
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor(output_mode=output_mode)
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}
        reason = {"reason": "invalid_input", "field": "value", "error": "must be numeric, got str in row 0"}

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            return (TransformResult.error(reason, retryable=False), [captured["token"]], "batch-1")

        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(factory.data_flow, "record_token_outcome_leader") as record_outcome,
            patch.object(processor._scheduler, "complete_barrier", wraps=processor._scheduler.complete_barrier) as complete_barrier,
            patch.object(processor, "_emit_token_completed", side_effect=RuntimeError("telemetry down")),
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert len(results) == 2
        _assert_outcome_pair(results[0], None, TerminalPath.BUFFERED)
        discarded = results[1]
        _assert_outcome_pair(discarded, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)
        assert discarded.sink_name is None
        assert discarded.token.token_id == captured["token"].token_id

        record_outcome.assert_not_called()
        complete_barrier.assert_called_once()
        barrier = complete_barrier.call_args.kwargs
        assert barrier["consumed_token_ids"] == (captured["token"].token_id,)
        assert barrier["emitted_pending_sink"] == ()
        assert barrier["terminal_outcomes"] == (
            BarrierTerminalOutcomeSpec(
                token_id=captured["token"].token_id,
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.QUARANTINED_AT_SOURCE,
                error_hash=compute_error_hash(canonical_json(reason)),
            ),
        )

    @pytest.mark.parametrize("output_mode", ["passthrough", "transform"])
    def test_flush_failure_with_named_on_error_hands_every_member_to_the_sink(self, output_mode: str) -> None:
        """elspeth-d2e3f29d10: a named aggregation on_error routes the batch.

        Every buffered member becomes a sink-bound (FAILURE, ON_ERROR_ROUTED)
        result carrying its ORIGINAL row and the batch reason, handed off
        BLOCKED -> PENDING_SINK in ONE complete_barrier (nothing consumed).
        No processor-side terminal is written: the sink records the one
        terminal per token after durability.
        """
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor(output_mode=output_mode, on_error="quarantine")
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}
        reason = {"reason": "invalid_input", "field": "value", "error": "must be numeric, got str in row 0"}

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            return (TransformResult.error(reason, retryable=False), [captured["token"]], "batch-1")

        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(factory.data_flow, "record_token_outcome_leader") as record_outcome,
            patch.object(processor._scheduler, "complete_barrier", wraps=processor._scheduler.complete_barrier) as complete_barrier,
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert len(results) == 2
        _assert_outcome_pair(results[0], None, TerminalPath.BUFFERED)
        routed = results[1]
        _assert_outcome_pair(routed, TerminalOutcome.FAILURE, TerminalPath.ON_ERROR_ROUTED)
        assert routed.sink_name == "quarantine"
        assert routed.scheduler_pending_sink is True
        assert routed.token.token_id == captured["token"].token_id
        assert routed.final_data == captured["token"].row_data
        assert routed.error is not None
        assert routed.error.message == canonical_json(reason), "the recorded reason text a resume reads back"

        record_outcome.assert_not_called()
        complete_barrier.assert_called_once()
        barrier = complete_barrier.call_args.kwargs
        assert barrier["consumed_token_ids"] == ()
        assert barrier["terminal_outcomes"] == ()
        [emission] = barrier["emitted_pending_sink"]
        assert emission.token_id == captured["token"].token_id
        assert (emission.sink_name, emission.outcome, emission.path) == ("quarantine", "failure", "on_error_routed")
        assert emission.error_message == canonical_json(reason)
        assert emission.error_hash == compute_error_hash(canonical_json(reason), exception_type="TransformError")

    def test_passthrough_success_with_rows_none_raises(self) -> None:
        """Passthrough flush requires rows list; rows=None is an invariant violation."""
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor(output_mode="passthrough")
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}

        # success_reason: the flush cross-check validates quarantine metadata in
        # both output modes before routing reaches the rows=None check.
        bad_result = SimpleNamespace(status="success", is_multi_row=True, rows=None, success_reason=None)

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            return bad_result, [captured["token"]], "batch-1"

        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(processor._data_flow, "record_token_outcome"),
            patch.object(processor, "_emit_transform_completed"),
            pytest.raises(FrameworkBugError, match=r"result\.rows None"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

    def test_passthrough_success_with_output_count_mismatch_raises(self) -> None:
        """Passthrough flush must return one output row per buffered input token.

        The mismatch is the plugin breaking its flush_emits_one_row_per_buffered_row
        declaration: every buffered token is recorded FAILURE / UNROUTED with the
        value-free shape evidence before the Tier-1 abort.
        """
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor(output_mode="passthrough")
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}

        mismatch_result = TransformResult.success_multi(
            [make_row({"value": 100}, contract=_make_contract())],
            success_reason={"action": "mismatch"},
        )

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        other_token_ids: list[str] = []

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            other_token = make_token_info(data={"value": 20})
            other_token_ids.append(other_token.token_id)
            return mismatch_result, [captured["token"], other_token], "batch-1"

        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(processor._data_flow, "record_token_outcome"),
            patch.object(processor._data_flow, "record_token_outcome_leader") as record_leader,
            patch.object(processor, "_emit_transform_completed"),
            pytest.raises(BatchPassthroughShapeError, match="same number of output rows") as raised,
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        assert isinstance(raised.value, OrchestrationInvariantError)
        assert raised.value.failure_kind == "row_count_mismatch"
        recorded = [call.kwargs for call in record_leader.call_args_list]
        assert [kwargs["ref"].token_id for kwargs in recorded] == [captured["token"].token_id, other_token_ids[0]]
        for kwargs in recorded:
            assert (kwargs["outcome"], kwargs["path"]) == (TerminalOutcome.FAILURE, TerminalPath.UNROUTED)
            assert kwargs["context"]["failure_kind"] == "row_count_mismatch"
            assert (kwargs["context"]["buffered_token_count"], kwargs["context"]["emitted_row_count"]) == (2, 1)
            assert "value" not in kwargs["context"]

    def test_timeout_flush_passthrough_with_downstream_returns_continuation_work(self) -> None:
        """Timeout flush atomically journals downstream continuation work."""
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        downstream_node = NodeID("downstream-2")
        agg_node = NodeID("agg-1")
        db, factory, processor, transform, agg_node = self._setup_batch_processor(
            output_mode="passthrough",
            node_to_next={NodeID("source-0"): agg_node, agg_node: downstream_node, downstream_node: None},
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        result = TransformResult.success_multi(
            [make_row({"value": 11}, contract=_make_contract())],
            success_reason={"action": "passthrough"},
        )
        buffered_token = make_token_info(data={"value": 10})
        _persist_blocked_scheduler_work(factory, processor, buffered_token, node_id=agg_node, barrier_key=str(agg_node))

        with patch.object(
            processor._aggregation_executor,
            "execute_flush",
            return_value=(result, [buffered_token], "batch-1"),
        ):
            results, child_items = processor.handle_timeout_flush(
                node_id=agg_node,
                transform=transform,
                ctx=ctx,
                trigger_type=TriggerType.TIMEOUT,
            )

        assert results == ()
        assert len(child_items) == 1
        assert child_items[0].current_node_id == downstream_node

        with db.connection() as conn:
            journal = conn.execute(
                select(
                    token_work_items_table.c.token_id,
                    token_work_items_table.c.node_id,
                    token_work_items_table.c.status,
                ).where(token_work_items_table.c.run_id == processor.run_id)
            ).all()

        assert {(row.token_id, row.node_id, row.status) for row in journal} == {
            (buffered_token.token_id, str(agg_node), "terminal"),
            (child_items[0].token.token_id, str(downstream_node), "ready"),
        }

    def test_timeout_flush_passthrough_terminal_returns_completed(self) -> None:
        """Timeout flush returns terminal COMPLETED results when no downstream/coalesce exists."""
        _db, factory, processor, transform, agg_node = self._setup_batch_processor(output_mode="passthrough")
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        result = TransformResult.success_multi(
            [make_row({"value": 11}, contract=_make_contract())],
            success_reason={"action": "passthrough"},
        )
        buffered_token = make_token_info(data={"value": 10})
        _persist_blocked_scheduler_work(factory, processor, buffered_token, node_id=agg_node, barrier_key=str(agg_node))

        with patch.object(
            processor._aggregation_executor,
            "execute_flush",
            return_value=(result, [buffered_token], "batch-1"),
        ):
            results, child_items = processor.handle_timeout_flush(
                node_id=agg_node,
                transform=transform,
                ctx=ctx,
                trigger_type=TriggerType.TIMEOUT,
            )

        assert child_items == []
        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].sink_name == "agg_sink"

    def test_transform_mode_triggering_token_quarantined_outcome(self) -> None:
        """Triggering token's RowResult must be QUARANTINED when quarantined_indices includes it.

        Regression: the triggering token (count-triggered flush) was unconditionally
        returned as CONSUMED_IN_BATCH in its RowResult, even when data_flow had
        already recorded it as QUARANTINED. This caused outcome disagreement between
        the audit trail and in-memory control flow.
        """
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor(output_mode="transform")
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}
        valid_buffered_token = make_token_info(row_id="row-a", token_id="token-valid", data={"value": 1})
        _persist_blocked_scheduler_work(factory, processor, valid_buffered_token, node_id=NodeID("agg-1"), barrier_key="agg-1")

        # The triggering token is the second buffered token. Index 1 in
        # quarantined_indices means the triggering token IS quarantined while
        # the batch still has a valid parent for emitted output.
        flush_result = TransformResult.success(
            make_row({"value": 999}, contract=_make_contract()),
            success_reason={
                "action": "batch_processed",
                "metadata": {"quarantined_indices": [1]},
            },
        )

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            return flush_result, [valid_buffered_token, captured["token"]], "batch-1"

        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(processor, "_emit_transform_completed"),
            patch.object(processor, "_emit_token_completed"),
            patch.object(processor._token_manager, "expand_token", return_value=([], "expand-group-1")) as expand_token,
        ):
            results = processor.process_row(
                row_index=1,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=1,
                ingest_sequence=1,
            )

        # The triggering token must be returned as QUARANTINED, matching the data_flow.
        triggering_results = [r for r in results if (r.outcome, r.path) == (TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)]
        assert len(triggering_results) == 1, (
            f"Expected triggering token RowResult with QUARANTINED outcome, got outcomes: {[r.outcome for r in results]}"
        )

        # Recorder must also have recorded the triggering token as QUARANTINED,
        # not CONSUMED_IN_BATCH. Other valid buffered tokens are still consumed.
        triggering_token_id = captured["token"].token_id
        recorded = [
            (
                disposition.parent_ref.token_id,
                disposition.outcome,
                disposition.path,
            )
            for disposition in expand_token.call_args.kwargs["aggregation_parent_dispositions"]
        ]
        assert (triggering_token_id, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE) in recorded
        assert (triggering_token_id, TerminalOutcome.TRANSIENT, TerminalPath.BATCH_CONSUMED) not in recorded

    def test_transform_mode_triggering_token_consumed_when_not_quarantined(self) -> None:
        """Triggering token's RowResult is CONSUMED_IN_BATCH when not in quarantined_indices.

        Companion to the quarantine test — verifies the normal (non-quarantined) path
        still works correctly after the fix.
        """
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor(output_mode="transform")
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}

        # No quarantined_indices — all tokens are consumed normally.
        flush_result = TransformResult.success(
            make_row({"value": 999}, contract=_make_contract()),
            success_reason={"action": "batch_processed"},
        )

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            return flush_result, [captured["token"]], "batch-1"

        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(factory.data_flow, "record_token_outcome") as record_outcome,
            patch.object(processor, "_emit_transform_completed"),
            patch.object(processor, "_emit_token_completed"),
            patch.object(processor._token_manager, "expand_token", return_value=([], "expand-group-1")) as expand_token,
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        # Slice 3 re-pin (ADR-030 §E.2): the trigger arrival surfaces as a
        # real (None, BUFFERED) RowResult and the intake-fired flush takes the
        # out-of-claim shape (triggering_token=None) — no extra
        # CONSUMED_IN_BATCH RowResult is emitted for the trigger member. Its
        # consumption lives in the audit trail.
        assert [(r.outcome, r.path) for r in results] == [(None, TerminalPath.BUFFERED)]

        # The expansion operation owns the selected parent's terminal outcome
        # atomically, so the processor must not issue a second recorder call.
        recorded_pairs = [(call.kwargs["outcome"], call.kwargs["path"]) for call in record_outcome.call_args_list]
        assert (TerminalOutcome.TRANSIENT, TerminalPath.BATCH_CONSUMED) not in recorded_pairs
        assert (TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE) not in recorded_pairs
        assert expand_token.call_args is not None
        assert expand_token.call_args.kwargs["parent_token"] == captured["token"]
        assert expand_token.call_args.kwargs["parent_path"] == TerminalPath.BATCH_CONSUMED
        assert expand_token.call_args.kwargs["parent_batch_id"] == "batch-1"

    def test_transform_mode_count_flush_expands_from_non_quarantined_parent(self) -> None:
        """Count-triggered batch output children must not use a quarantined triggering token as parent."""
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor(output_mode="transform")
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}
        valid_buffered_token = make_token_info(row_id="row-a", token_id="token-valid", data={"value": 1})
        _persist_blocked_scheduler_work(factory, processor, valid_buffered_token, node_id=NodeID("agg-1"), barrier_key="agg-1")

        flush_result = TransformResult.success(
            make_row({"value": 999}, contract=_make_contract()),
            success_reason={
                "action": "batch_processed",
                "metadata": {"quarantined_indices": [1]},
            },
        )

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            return flush_result, [valid_buffered_token, captured["token"]], "batch-1"

        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(factory.data_flow, "record_token_outcome"),
            patch.object(processor, "_emit_transform_completed"),
            patch.object(processor, "_emit_token_completed"),
            patch.object(processor._token_manager, "expand_token", return_value=([], "expand-group-1")) as expand_token,
        ):
            processor.process_row(
                row_index=1,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=1,
                ingest_sequence=1,
            )

        assert expand_token.call_args is not None
        assert expand_token.call_args.kwargs["parent_token"] == valid_buffered_token

    def test_transform_mode_timeout_flush_expands_from_non_quarantined_parent(self) -> None:
        """Timeout/end-of-source batch output children must not use a quarantined first buffered token as parent."""
        _db, factory, processor, transform, agg_node = self._setup_batch_processor(output_mode="transform")
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        quarantined_token = make_token_info(row_id="row-a", token_id="token-quarantined", data={"value": 1})
        valid_token = make_token_info(row_id="row-b", token_id="token-valid", data={"value": 2})
        _persist_blocked_scheduler_work(
            factory, processor, quarantined_token, node_id=agg_node, barrier_key=str(agg_node), ingest_sequence=0
        )
        _persist_blocked_scheduler_work(factory, processor, valid_token, node_id=agg_node, barrier_key=str(agg_node), ingest_sequence=1)

        flush_result = TransformResult.success(
            make_row({"value": 999}, contract=_make_contract()),
            success_reason={
                "action": "batch_processed",
                "metadata": {"quarantined_indices": [0]},
            },
        )

        with (
            patch.object(
                processor._aggregation_executor,
                "execute_flush",
                return_value=(flush_result, [quarantined_token, valid_token], "batch-1"),
            ),
            patch.object(factory.data_flow, "record_token_outcome"),
            patch.object(processor, "_emit_transform_completed"),
            patch.object(processor, "_emit_token_completed"),
            patch.object(processor._token_manager, "expand_token", return_value=([], "expand-group-1")) as expand_token,
        ):
            processor.handle_timeout_flush(
                node_id=agg_node,
                transform=transform,
                ctx=ctx,
                trigger_type=TriggerType.TIMEOUT,
            )

        assert expand_token.call_args is not None
        assert expand_token.call_args.kwargs["parent_token"] == valid_token

    def test_transform_mode_out_of_range_quarantined_index_fails_before_expansion(self) -> None:
        """Malformed batch quarantine metadata must fail before child tokens are created.

        The flush cross-check is the one site that validates the metadata; routing
        consumes the set it returns. The buffered token is recorded FAILURE /
        UNROUTED before the Tier-1 raise, so it does not end without an outcome.
        """
        db, factory, processor, transform, _agg_node = self._setup_batch_processor(output_mode="transform")
        token = make_token_info(row_id="row-a", token_id="token-a", data={"value": 1})
        _persist_token_for_scheduler(factory, token)
        fctx = _FlushContext(
            node_id=NodeID("agg-1"),
            transform=transform,
            settings=AggregationSettings(
                name="batch_agg",
                plugin="agg-transform",
                input="default",
                on_error="discard",
                trigger={"count": 1},
                output_mode="transform",
            ),
            buffered_tokens=(token,),
            batch_id="batch-1",
            expand_parent_token=token,
            triggering_token=token,
            coalesce_node_id=None,
            coalesce_name=None,
        )
        flush_result = TransformResult.success(
            make_row({"value": 999}, contract=_make_contract()),
            success_reason={
                "action": "batch_processed",
                "metadata": {"quarantined_indices": [1]},
            },
        )

        with (
            patch.object(processor._token_manager, "expand_token") as expand_token,
            pytest.raises(OrchestrationInvariantError, match="quarantined_indices"),
        ):
            processor._cross_check_flush_output(fctx, flush_result)

        expand_token.assert_not_called()

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_outcomes_table

        with db.connection() as conn:
            outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == "test-run")).fetchall()
        assert len(outcomes) == 1
        _assert_outcome_pair(outcomes[0], TerminalOutcome.FAILURE, TerminalPath.UNROUTED)
        assert outcomes[0].token_id == "token-a"
        assert json.loads(outcomes[0].context_json)["failure_kind"] == "quarantine_metadata_invalid"


# =============================================================================
# Committed-aggregation-output routing authority (elspeth-8783933d99)
# =============================================================================


class TestCommittedAggregationRoutingAuthority:
    """Nominal GateSettings rejection at the committed-output recovery seam.

    The bare ``cast(TransformProtocol, ...)`` this replaces let a gate at the
    receipt's aggregation node flow onward as a transform. The rejection is
    nominal (GateSettings), never protocol conformance — the closed
    node_to_plugin container makes "not a gate" mean "is a transform".
    """

    @staticmethod
    def _receipt(*, output_mode: str) -> CommittedAggregationOutputReceipt:
        return CommittedAggregationOutputReceipt(
            batch_id="batch-recov-1",
            aggregation_node_id="agg-1",
            aggregation_state_id="state-1",
            output_mode=output_mode,
            output_shape="single",
            output_hash=fake_sha256("deadbeef"),
            output_refs=(),
            member_token_ids=(),
            members=(),
            expansion_parent_token_id=None,
        )

    @staticmethod
    def _agg_settings() -> AggregationSettings:
        return AggregationSettings(
            name="recov-agg",
            plugin="test-plugin",
            input="agg_in",
            on_error="discard",
            trigger={"count": 3},
        )

    def test_gate_routing_authority_raises_audit_integrity(self) -> None:
        """A GateSettings at the receipt's aggregation node is refused by name.

        Mechanism pin: reverting the rejection to the bare cast lets the gate
        flow past this seam (it survives the mode check, since a gate has no
        say in output_mode) — this test then fails on the downstream wreckage
        instead of the honest AuditIntegrityError.
        """
        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        gate = GateSettings(name="not-a-transform", input="default", condition="True", routes={"true": "default", "false": "default"})
        settings = self._agg_settings()
        processor = _make_processor(
            factory,
            node_to_plugin={agg_node: gate},
            aggregation_settings={agg_node: settings},
        )

        with pytest.raises(AuditIntegrityError, match="resolves to gate"):
            processor._build_committed_aggregation_output_context(
                self._receipt(output_mode=settings.output_mode.value),
                [],
                [],
            )

    def test_non_conforming_transform_flows_past_gate_rejection(self) -> None:
        """Control: a transform missing a protocol member is NOT gate-rejected.

        The fake reaches the NEXT integrity check (output-mode disagreement),
        proving the rejection keys nominally on GateSettings rather than on
        TransformProtocol conformance.
        """
        from tests.fixtures.nonconforming_transform import NonConformingTransform

        _db, factory = _make_factory()
        agg_node = NodeID("agg-1")
        transform = NonConformingTransform(node_id="agg-1", is_batch_aware=True)
        assert not isinstance(transform, TransformProtocol)  # precondition, not the pin
        settings = self._agg_settings()
        assert settings.output_mode.value != "passthrough"
        processor = _make_processor(
            factory,
            node_to_plugin={agg_node: transform},
            aggregation_settings={agg_node: settings},
        )

        with pytest.raises(AuditIntegrityError, match="disagrees with current graph mode"):
            processor._build_committed_aggregation_output_context(
                self._receipt(output_mode="passthrough"),
                [],
                [],
            )


# =============================================================================
# Aggregation facades (thin delegation tests)
# =============================================================================


class TestAggregationFacades:
    """Tests for the aggregation public facade methods."""

    def test_check_aggregation_timeout_delegates(self) -> None:
        """check_aggregation_timeout delegates to aggregation_executor."""
        _, factory = _make_factory()
        processor = _make_processor(factory)

        with patch.object(
            processor._aggregation_executor,
            "check_flush_status",
            return_value=(True, TriggerType.TIMEOUT),
        ):
            should_flush, trigger = processor.check_aggregation_timeout(NodeID("agg-1"))

        assert should_flush is True
        assert trigger == TriggerType.TIMEOUT

    def test_get_aggregation_buffer_count_delegates(self) -> None:
        """get_aggregation_buffer_count delegates to aggregation_executor."""
        _, factory = _make_factory()
        processor = _make_processor(factory)

        with patch.object(
            processor._aggregation_executor,
            "get_buffer_count",
            return_value=5,
        ):
            count = processor.get_aggregation_buffer_count(NodeID("agg-1"))

        assert count == 5

    def test_get_barrier_scalars_composes_aggregation_executor(self) -> None:
        """get_barrier_scalars composes the aggregation executor's latches (F1 Task 2.4).

        Aggregation entries are re-keyed by str(node_id); with no coalesce
        executor wired, the coalesce side is empty.
        """
        from elspeth.contracts.barrier_scalars import AggregationNodeScalars, BarrierScalars

        _, factory = _make_factory()
        processor = _make_processor(factory)

        node_scalars = AggregationNodeScalars(count_fire_offset=0.5, condition_fire_offset=None)
        with patch.object(
            processor._aggregation_executor,
            "get_barrier_scalars",
            return_value={NodeID("agg-1"): node_scalars},
        ):
            result = processor.get_barrier_scalars()

        assert isinstance(result, BarrierScalars)
        assert dict(result.aggregation) == {"agg-1": node_scalars}
        assert dict(result.coalesce) == {}  # no coalesce executor → empty

    def test_get_barrier_scalars_composes_coalesce_executor(self) -> None:
        """get_barrier_scalars folds in the coalesce executor's lost-branch scalars."""
        from elspeth.contracts.barrier_scalars import BarrierScalars, CoalescePendingScalars

        _, factory = _make_factory()
        coalesce_executor = create_autospec(CoalesceExecutor, instance=True)
        pending_scalars = CoalescePendingScalars(lost_branches={"branch_b": "transform_failed"})
        coalesce_executor.get_barrier_scalars.return_value = {("merge", "row-1"): pending_scalars}
        processor = _make_processor(factory, coalesce_executor=coalesce_executor)

        with patch.object(
            processor._aggregation_executor,
            "get_barrier_scalars",
            return_value={},
        ):
            result = processor.get_barrier_scalars()

        assert isinstance(result, BarrierScalars)
        assert dict(result.aggregation) == {}
        assert dict(result.coalesce) == {("merge", "row-1"): pending_scalars}


# =============================================================================
# Regression: hscm.1 — Terminal deaggregation children inherit correct sink
# =============================================================================


class TestTerminalDeaggregationSinkRouting:
    """Regression tests for hscm.1: terminal deagg children must inherit the
    terminal transform's on_success sink, not source_on_success."""

    def test_terminal_deagg_children_use_transform_on_success_not_source(self) -> None:
        """Children of a terminal multi-row transform must route to transform's on_success."""
        _db, factory = _make_factory()
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        contract = _make_contract()
        output_rows = [
            make_row({"value": 1}, contract=contract),
            make_row({"value": 2}, contract=contract),
        ]
        multi_result = TransformResult.success_multi(
            output_rows,
            success_reason={"action": "expand"},
        )

        # Key: transform on_success != source_on_success
        transform = _make_mock_transform(
            creates_tokens=True,
            on_success="transform_sink",
        )
        source_node = NodeID("source-0")
        transform_node = NodeID(transform.node_id)
        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            node_step_map={source_node: 0, transform_node: 1},
            node_to_next={source_node: transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (multi_result, token, None)

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=executor_side_effect,
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        # Parent should be EXPANDED (no sink_name)
        expanded = [r for r in results if (r.outcome, r.path) == (TerminalOutcome.TRANSIENT, TerminalPath.EXPAND_PARENT)]
        assert len(expanded) == 1

        # Children should be COMPLETED with transform's on_success sink
        completed = [r for r in results if (r.outcome, r.path) == (TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)]
        assert len(completed) == 2
        for r in completed:
            assert r.sink_name == "transform_sink", (
                f"Expected 'transform_sink' but got '{r.sink_name}'. "
                f"Terminal deagg children must inherit the transform's on_success, "
                f"not source_on_success."
            )

    def test_mid_chain_deagg_children_process_through_remaining_transforms(self) -> None:
        """Mid-chain multi-row expansion: children continue to downstream transforms."""
        _db, factory = _make_factory()
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        contract = _make_contract()
        output_rows = [
            make_row({"value": 10}, contract=contract),
            make_row({"value": 20}, contract=contract),
        ]
        multi_result = TransformResult.success_multi(
            output_rows,
            success_reason={"action": "expand"},
        )
        single_result = TransformResult.success(
            make_row({"value": 99}, contract=contract),
            success_reason={"action": "passthrough"},
        )

        # First transform expands, second is terminal
        expander = _make_mock_transform(
            node_id="expander-1",
            name="expander",
            creates_tokens=True,
            on_success=None,  # mid-chain, no on_success needed
        )
        terminal = _make_mock_transform(
            node_id="terminal-2",
            name="terminal",
            creates_tokens=False,
            on_success="final_sink",
        )

        source_node = NodeID("source-0")
        expander_node = NodeID("expander-1")
        terminal_node = NodeID("terminal-2")

        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            node_step_map={source_node: 0, expander_node: 1, terminal_node: 2},
            node_to_next={source_node: expander_node, expander_node: terminal_node, terminal_node: None},
            node_to_plugin={expander_node: expander, terminal_node: terminal},
        )

        call_count = 0

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            nonlocal call_count
            call_count += 1
            if transform.name == "expander":
                return (multi_result, token, None)
            return (single_result, token, None)

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=executor_side_effect,
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[expander, terminal],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        # Should have 1 EXPANDED + 2 COMPLETED (children processed through terminal)
        completed = [r for r in results if (r.outcome, r.path) == (TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)]
        assert len(completed) == 2
        for r in completed:
            assert r.sink_name == "final_sink"

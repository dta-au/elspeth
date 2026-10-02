"""An out-of-claim barrier release advances every continuation in ONE drain.

``RowProcessor.drain_released_continuations`` is the one authority for the
continuations of a release made outside a claim: a timeout or end-of-source
aggregation flush (``orchestrator/aggregation.py``, pinned end to end by
``tests/integration/pipeline/test_aggregation_release_continuations.py``) and the
orchestrator's end-of-input intake, ``run_barrier_intake``, pinned here.

The shape is a takeover leader whose first intake adopts two BLOCKED rows the
prior leader deposited at a count-2 passthrough aggregation. The trigger fires
inside the intake, and the flush releases both tokens READY to a transform that
adds a field, which feeds a second aggregation. Driving each continuation
through its own drain let the first drain also advance the sibling. The sibling
then blocked at the second aggregation carrying the transform's row, and its own
drain's enqueue replayed the stale READY image against that row and aborted.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from elspeth.contracts import TransformResult
from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.contracts.types import NodeID
from elspeth.core.config import AggregationSettings
from elspeth.core.landscape.schema import token_work_items_table
from elspeth.testing import make_token_info
from tests.fixtures.factories import make_context
from tests.unit.engine.test_processor import (
    BarrierJournalRestoreContext,
    _make_factory,
    _make_mock_transform,
    _make_processor,
    _persist_blocked_scheduler_work,
    _persist_token_for_scheduler,
)

_FIRST_AGG = NodeID("agg-release")
_TAG = NodeID("transform-tag")
_SECOND_AGG = NodeID("agg-downstream")


def _passthrough_batch(node_id: NodeID) -> Any:
    transform = _make_mock_transform(node_id=str(node_id), name=f"batch-{node_id}", is_batch_aware=True, on_success="out")

    def process(rows: list[PipelineRow], ctx: Any) -> TransformResult:
        shared_contract = rows[0].contract
        return TransformResult.success_multi(
            [PipelineRow(row.to_dict(), shared_contract) for row in rows],
            success_reason={"action": "passthrough"},
        )

    transform.process.side_effect = process
    return transform


def _tagging_transform() -> Any:
    transform = _make_mock_transform(node_id=str(_TAG), name="tag")
    transform._on_start_called = True

    def process(row: PipelineRow, ctx: Any) -> TransformResult:
        return TransformResult.success(PipelineRow(dict(row.to_dict(), tagged=True), row.contract), success_reason={"action": "tag"})

    transform.process.side_effect = process
    return transform


def _aggregation(name: str, count: int) -> AggregationSettings:
    return AggregationSettings(
        name=name,
        plugin=f"batch-{name}",
        input="default",
        on_error="discard",
        trigger={"count": count},
        output_mode="passthrough",
    )


def test_an_intake_release_advances_both_continuations_in_one_drain() -> None:
    db, factory = _make_factory()
    processor = _make_processor(
        factory,
        node_step_map={NodeID("source-0"): 0, _FIRST_AGG: 1, _TAG: 2, _SECOND_AGG: 3},
        node_to_next={NodeID("source-0"): _FIRST_AGG, _FIRST_AGG: _TAG, _TAG: _SECOND_AGG, _SECOND_AGG: None},
        node_to_plugin={
            _FIRST_AGG: _passthrough_batch(_FIRST_AGG),
            _TAG: _tagging_transform(),
            _SECOND_AGG: _passthrough_batch(_SECOND_AGG),
        },
        aggregation_settings={_FIRST_AGG: _aggregation("release", 2), _SECOND_AGG: _aggregation("downstream", 100)},
        barrier_restore=BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-takeover", barrier_scalars=None, batch_id_remap={}),
    )
    ctx = make_context(
        landscape=factory.plugin_audit_writer(),
        coordination_token=processor._require_coordination_token(),
        member_token=processor._require_member_token(),
    )
    # The prior leader deposited both holds and died before adopting them.
    for ingest_sequence, token_id in enumerate(("tok-1", "tok-2")):
        token = make_token_info(row_id=f"row-{token_id}", token_id=token_id, data={"value": ingest_sequence})
        _persist_blocked_scheduler_work(
            factory,
            processor,
            token,
            node_id=_FIRST_AGG,
            barrier_key=str(_FIRST_AGG),
            ingest_sequence=ingest_sequence,
            adopted=False,
        )

    results = processor.run_barrier_intake(ctx)

    # Both continuations crossed the transform and are held at the second aggregation.
    assert sorted((result.token.token_id, result.outcome, result.path) for result in results) == [
        ("tok-1", None, TerminalPath.BUFFERED),
        ("tok-2", None, TerminalPath.BUFFERED),
    ]
    assert processor.get_aggregation_buffer_count(_SECOND_AGG) == 2
    with db.connection() as conn:
        tag_rows = conn.execute(
            select(token_work_items_table.c.token_id, token_work_items_table.c.status, token_work_items_table.c.barrier_key).where(
                token_work_items_table.c.node_id == str(_TAG)
            )
        ).all()
    assert sorted(tag_rows) == [
        ("tok-1", TokenWorkStatus.BLOCKED.value, str(_SECOND_AGG)),
        ("tok-2", TokenWorkStatus.BLOCKED.value, str(_SECOND_AGG)),
    ]


def test_an_empty_release_drains_nothing() -> None:
    """With no continuation, the call must not enter a claim loop that would advance unrelated READY work."""
    db, factory = _make_factory()
    processor = _make_processor(factory)
    ctx = make_context(
        landscape=factory.plugin_audit_writer(),
        coordination_token=processor._require_coordination_token(),
        member_token=processor._require_member_token(),
    )
    token = make_token_info(row_id="row-1", token_id="tok-1", data={"value": 1})
    _persist_token_for_scheduler(factory, token)
    unrelated = processor._scheduler.enqueue_ready(
        member_token=processor._require_member_token(),
        token_id=token.token_id,
        row_id=token.row_id,
        node_id="source-0",
        step_index=0,
        ingest_sequence=0,
        row_payload_json=processor._scheduler.serialize_row_payload(token.row_data),
    )

    assert processor.drain_released_continuations((), ctx) == []
    with db.connection() as conn:
        status = conn.execute(
            select(token_work_items_table.c.status).where(token_work_items_table.c.work_item_id == unrelated.work_item_id)
        ).scalar_one()
    assert status == TokenWorkStatus.READY.value

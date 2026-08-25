"""A BLOCKED collector member is barrier work at the PROCESSOR surface (spec §5 / §9 row 5).

``RowProcessor.has_blocked_barrier_work`` is the §D step-3 loop condition of
``run_end_of_input_barrier_flush``. A collector has no flush arm by design
(end_of_group only, no trigger — B9 ruling), so the ONLY thing keeping the
EOF fixpoint alive until a collector roster settles is that every buffered
member holds a durable BLOCKED journal row with a non-NULL ``barrier_key``.
If a collector hold does not count here, the loop exits its fixpoint before
the group closes and the member is silently stranded.

``tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py``
pins the loop against an attribute-bag processor double; this module drives
the REAL ``RowProcessor`` over a real journal, then threads that real
processor through the real loop to read integration C2's landed guard
(``collector_executor is None`` in the early return, and the collector
hold count in the non-convergence diagnostic) — WS5 Task 4's verification
that C2 landed, read from behaviour rather than from the source text.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.types import NodeID
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.engine.orchestrator.leader_drain import run_end_of_input_barrier_flush
from elspeth.engine.orchestrator.types import ExecutionCounters
from elspeth.engine.processor import RowProcessor
from elspeth.testing import make_token_info
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import leader_coordination_token
from tests.unit.engine.test_processor import _make_factory, _make_processor, _persist_token_for_scheduler

_COLLECTOR_NAME = "stitch"
_EXPAND_GROUP_ID = "expand-1"
_COLLECTOR_NODE = NodeID("collector::stitch")
_LEASE_OWNER = "test-harness"


class _CollectorExecutorDouble:
    """Only the surface the EOF drain reads off a collector executor."""

    def __init__(self, *, buffered: int) -> None:
        self.buffered = buffered

    def buffered_member_count(self) -> int:
        return self.buffered


def _collector_processor(factory: RecorderFactory, *, collector_executor: Any = None) -> RowProcessor:
    return _make_processor(
        factory,
        node_step_map={NodeID("source-0"): 0, _COLLECTOR_NODE: 1},
        node_to_next={NodeID("source-0"): _COLLECTOR_NODE, _COLLECTOR_NODE: None},
        collector_executor=collector_executor,
    )


def _member_token(ordinal: int) -> Any:
    return make_token_info(
        row_id=f"row-{ordinal}",
        token_id=f"member-{ordinal}",
        data={"value": ordinal},
        lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id=_EXPAND_GROUP_ID, member_key=f"member-{ordinal}"),),
    )


def _enqueue_claimed(factory: RecorderFactory, processor: RowProcessor, token: Any, *, ordinal: int, collector_name: str | None) -> Any:
    """Enqueue+claim one member at the collector node via the production journal verbs."""
    _persist_token_for_scheduler(factory, token, ingest_sequence=ordinal)
    now = processor._clock.now_utc()
    return processor._scheduler.enqueue_ready_claimed_legacy_unfenced(
        run_id=processor.run_id,
        token_id=token.token_id,
        row_id=token.row_id,
        node_id=str(_COLLECTOR_NODE),
        step_index=processor.resolve_node_step(_COLLECTOR_NODE),
        ingest_sequence=ordinal,
        row_payload_json=processor._scheduler.serialize_row_payload(token.row_data),
        available_at=now,
        lease_owner=_LEASE_OWNER,
        lease_seconds=60,
        now=now,
        lineage_path=token.lineage_path,
        collector_name=collector_name,
    )


def _persist_blocked_collector_member(factory: RecorderFactory, processor: RowProcessor, *, ordinal: int) -> str:
    """A collector member hold exactly as the live intake journals it: the
    ``collector_name`` cursor column set and the COMPOUND
    ``collector_barrier_key(name, group_id)`` as the barrier address."""
    item = _enqueue_claimed(factory, processor, _member_token(ordinal), ordinal=ordinal, collector_name=_COLLECTOR_NAME)
    processor._scheduler.mark_blocked(
        work_item_id=item.work_item_id,
        queue_key=None,
        barrier_key=collector_barrier_key(_COLLECTOR_NAME, _EXPAND_GROUP_ID),
        now=processor._clock.now_utc(),
        expected_lease_owner=_LEASE_OWNER,
    )
    return str(item.work_item_id)


def _persist_blocked_queue_hold(factory: RecorderFactory, processor: RowProcessor, *, ordinal: int) -> None:
    """The dual-use BLOCKED partition's OTHER occupant (F1 design D1): an
    ADR-028 queue-hold — BLOCKED with a ``queue_key`` and NO ``barrier_key``."""
    item = _enqueue_claimed(factory, processor, _member_token(ordinal), ordinal=ordinal, collector_name=None)
    processor._scheduler.mark_blocked(
        work_item_id=item.work_item_id,
        queue_key=str(_COLLECTOR_NODE),
        barrier_key=None,
        now=processor._clock.now_utc(),
        expected_lease_owner=_LEASE_OWNER,
    )


def test_blocked_collector_member_counts_as_barrier_work() -> None:
    _, factory = _make_factory()
    processor = _collector_processor(factory)
    assert processor.has_blocked_barrier_work() is False  # control: an empty journal is not barrier work

    _persist_blocked_collector_member(factory, processor, ordinal=0)

    assert processor.has_blocked_barrier_work() is True


def test_settled_collector_member_stops_counting_as_barrier_work() -> None:
    """The zero-write direction: once the hold is released terminally the
    surface goes False again, so the fixpoint can exit — a surface that
    counted collector rows regardless of status would never converge."""
    _, factory = _make_factory()
    processor = _collector_processor(factory)
    _persist_blocked_collector_member(factory, processor, ordinal=0)
    assert processor.has_blocked_barrier_work() is True

    released = processor._scheduler.mark_blocked_barrier_terminal(
        run_id=processor.run_id,
        barrier_key=collector_barrier_key(_COLLECTOR_NAME, _EXPAND_GROUP_ID),
        token_ids=("member-0",),
        now=processor._clock.now_utc(),
        coordination_token=leader_coordination_token(factory, processor.run_id),
    )
    assert released == 1

    assert processor.has_blocked_barrier_work() is False


def test_queue_hold_alone_is_not_barrier_work() -> None:
    """BLOCKED is dual-use (D1): a queue-hold is BLOCKED too, but it carries
    no ``barrier_key`` and must NOT keep the EOF barrier fixpoint alive."""
    _, factory = _make_factory()
    processor = _collector_processor(factory)

    _persist_blocked_queue_hold(factory, processor, ordinal=0)

    assert processor.has_blocked_barrier_work() is False


# ---------------------------------------------------------------------------
# Integration C2, read from behaviour: the real processor through the real
# EOF loop. No aggregation, no coalesce, no row_union — only the collector
# guard can keep the loop from early-returning.
# ---------------------------------------------------------------------------


class _CollectorOnlyConfig:
    aggregation_settings: ClassVar[dict[str, Any]] = {}
    escalation_fixpoint_bound = 3


def _run_eof_flush(processor: RowProcessor) -> None:
    run_end_of_input_barrier_flush(
        config=_CollectorOnlyConfig(),  # type: ignore[arg-type]
        processor=processor,
        ctx=make_context(),
        counters=ExecutionCounters(),
        pending_tokens={},
        coalesce_executor=None,
        coalesce_node_map={},
    )


def _count_intake_passes(processor: RowProcessor, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Stub the intake pass (a fabricated hold has no executor memory to
    replay) and count it: one call per loop iteration, zero on early return."""
    calls = {"count": 0}

    def _counting_intake(ctx: Any) -> list[Any]:
        calls["count"] += 1
        return []

    monkeypatch.setattr(processor, "run_barrier_intake", _counting_intake)
    return calls


def test_eof_loop_stays_alive_on_a_real_blocked_collector_hold_and_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _, factory = _make_factory()
    processor = _collector_processor(factory, collector_executor=_CollectorExecutorDouble(buffered=2))
    _persist_blocked_collector_member(factory, processor, ordinal=0)
    _persist_blocked_collector_member(factory, processor, ordinal=1)
    calls = _count_intake_passes(processor, monkeypatch)

    with pytest.raises(
        OrchestrationInvariantError,
        match=r"did not converge within 3 intake/flush rounds.*Collector members still buffered in memory: 2",
    ):
        _run_eof_flush(processor)

    # Every round ran: the guard did not early-return a collector-only
    # pipeline, and the real has_blocked_barrier_work held the loop open.
    assert calls["count"] == 3


def test_eof_loop_exits_once_the_real_collector_hold_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settle the hold from inside the (stubbed) intake pass, exactly where
    production settles it, and the real loop condition lets the loop return."""
    _, factory = _make_factory()
    processor = _collector_processor(factory, collector_executor=_CollectorExecutorDouble(buffered=1))
    _persist_blocked_collector_member(factory, processor, ordinal=0)
    calls = {"count": 0}

    def _settling_intake(ctx: Any) -> list[Any]:
        calls["count"] += 1
        processor._scheduler.mark_blocked_barrier_terminal(
            run_id=processor.run_id,
            barrier_key=collector_barrier_key(_COLLECTOR_NAME, _EXPAND_GROUP_ID),
            token_ids=("member-0",),
            now=processor._clock.now_utc(),
            coordination_token=leader_coordination_token(factory, processor.run_id),
        )
        return []

    monkeypatch.setattr(processor, "run_barrier_intake", _settling_intake)

    _run_eof_flush(processor)

    assert calls["count"] == 1
    assert processor.has_blocked_barrier_work() is False


def test_eof_loop_early_returns_when_no_barrier_executor_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control for the guard: a real processor with NO executors of any kind
    still skips the loop, even with a stray BLOCKED barrier row — the guard
    is keyed on the executors, and the collector arm added nothing else."""
    _, factory = _make_factory()
    processor = _collector_processor(factory, collector_executor=None)
    _persist_blocked_collector_member(factory, processor, ordinal=0)
    calls = _count_intake_passes(processor, monkeypatch)

    _run_eof_flush(processor)

    assert calls["count"] == 0

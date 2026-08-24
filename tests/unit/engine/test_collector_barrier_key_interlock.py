"""WS4 Task 6 / META-14.2 (C-2): the collector barrier_key interlock.

barrier_key = "collector:<collector_name>:<group_id>" is landed as a
CONSTANT/CONVENTION by this task (spec §4.3), but no production writer
constructs one yet — the WS3+WS4 integration item's intake arm
(BarrierIntakeCoordinator) and the processor's mark_blocked call for
collector arrivals are the writers, and neither is wired until then
(META-4/META-10.1/META-14.3).

This makes today's accidental "no collector barrier_key row can exist"
property into an EXPLICIT, pinned interlock (the accidental interlock
"becomes EXPLICIT" per the META-14 ruling):

1. test_no_production_writer_of_collector_barrier_keys_exists_yet — a
   whole-tree canary: fails loudly the moment a future commit adds a real
   writer, so that commit is forced to also address (2) rather than
   silently reaching a code path this repo has never exercised.
2. test_orphan_collector_barrier_key_is_the_current_fail_closed_state pins
   that IF such a row existed today, BarrierIntakeCoordinator's intake
   classifier (barrier_coordination.py's run_intake_pass, WS3-owned — this
   comment lives here, not there, per the shared-checkout lane split) would
   refuse it with an "orphan barrier_key" AuditIntegrityError, because the
   classifier only recognizes configured coalesce/row_union/aggregation
   keys — collector is not in its vocabulary pre-integration. This is the
   CURRENT deliberate fail-closed state, not a bug: it is what stops a
   would-be premature collector arrival from being silently misrouted
   before the integration item's intake classifier arm lands.

Both tests must be revisited together when integration wires a real
collector barrier_key writer: (1) will start failing (by design — that is
its whole purpose) and (2) will need a positive-path sibling once the
classifier gains a collector arm.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.types import NodeID
from elspeth.engine.barrier_coordination import BarrierIntakeCoordinator
from elspeth.engine.clock import MockClock
from elspeth.engine.work_items import WorkItemFactory
from tests.unit.engine.test_barrier_coordination import (
    FakeNav,
    RecordingAggregationExecutor,
    RecordingScheduler,
    _batch_aware_transform,
    _blocked_row,
    _ctx,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
_AGG_NODE = NodeID("agg-node")


def _is_collector_prefixed_string(node: ast.expr) -> bool:
    """True for a literal or f-string node whose value starts with 'collector:'."""
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("collector:")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("collector:")
    return False


def test_no_production_writer_of_collector_barrier_keys_exists_yet() -> None:
    """Scans every src/elspeth/**/*.py for a barrier_key= call-keyword or
    assignment constructed from a "collector:"-prefixed literal/f-string.

    Deliberately narrower than "any 'collector:'-prefixed string anywhere"
    — core/dag/builder.py:723 legitimately builds an unrelated schema-error
    owner label (f"collector:{collector_name}", not a barrier_key) that a
    naive whole-string scan would false-positive on. Only assignments to a
    variable/attribute/kwarg literally named barrier_key count.
    """
    hits: list[tuple[str, int]] = []
    for path in sorted((REPO_ROOT / "src" / "elspeth").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "barrier_key" and _is_collector_prefixed_string(kw.value):
                        hits.append((str(path.relative_to(REPO_ROOT)), node.lineno))
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is not None and _is_collector_prefixed_string(value):
                    for target in targets:
                        name = getattr(target, "attr", None) or getattr(target, "id", None)
                        if name == "barrier_key":
                            hits.append((str(path.relative_to(REPO_ROOT)), node.lineno))
    assert hits == [], (
        "A production writer of collector barrier_keys now exists "
        f"({hits!r}) — this interlock test's whole premise (no writer exists "
        "pre-integration) is now false. It must be REMOVED (not weakened) in "
        "the same commit that adds the writer, per META-14.2's self-destruct "
        "note (integration worklist item 15): confirm the new writer's "
        "barrier_key matches the collector:<name>:<group_id> convention and "
        "that BarrierIntakeCoordinator's classifier gained a matching arm "
        "(see test_orphan_collector_barrier_key_is_the_current_fail_closed_state "
        "in this file for the fail-closed test it must supersede)."
    )


def _make_aggregation_only_coordinator(*, scheduler: RecordingScheduler) -> BarrierIntakeCoordinator:
    """Minimal local construction, NOT test_barrier_coordination._make_coordinator.

    That helper (and its whole module) is WS3's actively-dirty lane —
    reusing it directly hit a live mid-edit inconsistency
    (BarrierIntakeCoordinator gained a new required
    take_pending_group_losses parameter that _make_coordinator's own
    call site had not caught up with yet, confirmed independently: WS3's
    own test_barrier_coordination.py::TestIntakeFailClosed::test_orphan_barrier_key_raises
    fails the same way at the time of writing). Only the fields the orphan-raise
    path actually needs are supplied — aggregation-only, no coalesce/row_union
    — trimmed from _make_coordinator's shape rather than duplicating its
    full generality, to minimize this file's coupling to that module's churn.
    """
    resolved_nav = FakeNav(transform=_batch_aware_transform())
    restore_reads = SimpleNamespace(
        get_max_node_state_attempts=lambda run_id, token_ids: {},
        row_id_for_token=lambda run_id, token_id: "row-1",
    )
    return BarrierIntakeCoordinator(
        run_id="run-1",
        scheduler=scheduler,
        data_flow=SimpleNamespace(record_token_outcome=lambda **kwargs: None),
        execution=SimpleNamespace(),
        barrier_restore_reads=restore_reads,
        aggregation_executor=RecordingAggregationExecutor(),
        coalesce_executor=None,
        nav=resolved_nav,
        work_items=WorkItemFactory(resolved_nav),
        clock=MockClock(start=100.0),
        aggregation_settings={_AGG_NODE: object()},
        coalesce_node_ids={},
        branch_to_coalesce={},
        coordination_token=SimpleNamespace(worker_id="leader-1", epoch=1),
        scheduler_lease_owner="leader-1",
        live_barrier_holds={},
        resume_checkpoint_id=None,
        flush_batch=lambda node_id, transform, ctx, trigger_type: ((), []),
        complete_coalesce_fire=lambda **kwargs: None,
        terminal_coalesce_row_result=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("not reached")),
        emit_token_completed=lambda token, *, outcome, path, sink_name=None: None,
        mark_coalesce_consumed_terminal=lambda *, coalesce_name, consumed_tokens: None,
        record_group_member_terminals=lambda *args, **kwargs: [],
        take_pending_group_losses=lambda: (),
    )


class TestCollectorBarrierKeyFailClosed:
    def test_orphan_collector_barrier_key_is_the_current_fail_closed_state(self) -> None:
        row = _blocked_row(barrier_key="collector:stitch:g-1")
        scheduler = RecordingScheduler(pending=[row])
        coordinator = _make_aggregation_only_coordinator(scheduler=scheduler)

        with pytest.raises(AuditIntegrityError, match="orphan barrier_key"):
            coordinator.run_intake_pass(_ctx())

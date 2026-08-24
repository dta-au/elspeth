"""WS4 Task 6 / META-14.2 (C-2): the collector barrier_key interlock.

``collector_barrier_key(name, group_id)`` (core/landscape/scheduler/
work_items.py) is THE single named construction site for the compound
"collector:<collector_name>:<group_id>" address (spec §4.3) — landed as a
CONSTANT/CONVENTION by this task, but no production writer calls it yet:
the WS3+WS4 integration item's intake arm (BarrierIntakeCoordinator) and
the processor's mark_blocked call for collector arrivals are the writers,
and neither is wired until then (META-4/META-10.1/META-14.3).

This makes today's accidental "no collector barrier_key row can exist"
property into an EXPLICIT, pinned interlock (the accidental interlock
"becomes EXPLICIT" per the META-14 ruling):

1. test_no_production_writer_of_collector_barrier_keys_exists_yet — a
   whole-tree canary retargeted (I-2, fix round) at CALLS to
   collector_barrier_key rather than bare-literal/f-string pattern
   matching, since the latter is trivially evaded by a helper call,
   string concatenation, or .format(). Fails loudly the moment a future
   commit calls the helper from src/elspeth/, so that commit is forced to
   also address (2) rather than silently reaching a code path this repo
   has never exercised.
2. test_orphan_collector_barrier_key_is_the_current_fail_closed_state pins
   that IF such a row existed today, BarrierIntakeCoordinator's intake
   classifier (barrier_coordination.py's run_intake_pass, WS3-owned — this
   comment lives here, not there, per the shared-checkout lane split) would
   refuse it with an "orphan barrier_key" AuditIntegrityError, because the
   classifier only recognizes configured coalesce/row_union/aggregation
   keys — collector is not in its vocabulary pre-integration. This is the
   CURRENT deliberate fail-closed state, not a bug: it is what stops a
   would-be premature collector arrival from being silently misrouted
   before the integration item's intake classifier arm lands. M-2 (fix
   round): this test ALSO asserts BarrierIntakeCoordinator.__init__ has no
   collector-shaped parameter yet, so it fails loudly — not silently
   keeps "passing" for the wrong reason — the moment integration adds one,
   even before anyone remembers to update this file's minimal fixture to
   configure it.

Both tests must be revisited together when integration wires a real
collector barrier_key writer: (1) will start failing (by design — that is
its whole purpose) and (2) will need a positive-path sibling once the
classifier gains a collector arm.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.types import NodeID
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
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
_HELPER_NAME = collector_barrier_key.__name__


def _call_target_name(func: ast.expr) -> str | None:
    """The called symbol's bare name for a Name or Attribute call target, else None.

    Structural narrowing (isinstance over ast.expr's closed subclass set),
    not a dynamic-attribute probe — mirrors _assignment_target_name's
    reasoning in the sibling scan this replaces (removed, I-2): only
    ast.Name.id (bare `collector_barrier_key(...)`) and ast.Attribute.attr
    (`work_items.collector_barrier_key(...)`) are read, each after the
    node's own type confirms the field exists.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_no_production_writer_of_collector_barrier_keys_exists_yet() -> None:
    """Scans every src/elspeth/**/*.py for a CALL to collector_barrier_key.

    I-2 (fix round): retargeted from bare-literal/f-string pattern matching
    (the original shape) to watching the named construction site itself —
    a helper call, however the collector_name/group_id arguments are
    computed, cannot evade a call-site scan the way string concatenation
    or .format() could evade a literal-shape scan.
    """
    hits: list[tuple[str, int]] = []
    for path in sorted((REPO_ROOT / "src" / "elspeth").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_target_name(node.func) == _HELPER_NAME:
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
        # M-2 (fix round): symmetric self-destruct. Without this, the test
        # below would keep "passing" for the WRONG reason the moment
        # integration adds collector support to BarrierIntakeCoordinator —
        # _make_aggregation_only_coordinator's minimal fixture simply never
        # populates a newly-added collector-shaped parameter, so the
        # coordinator would fall through to this SAME orphan-raise for
        # "not configured in THIS fixture" reasons rather than "not in the
        # classifier's vocabulary at all" reasons. Assert the current
        # constructor signature has no such parameter FIRST — this fails
        # loudly the instant integration adds one, forcing a revisit of
        # both the fixture and this test rather than a silent stale pass.
        coordinator_params = set(inspect.signature(BarrierIntakeCoordinator.__init__).parameters)
        collector_params = sorted(name for name in coordinator_params if "collector" in name)
        assert not collector_params, (
            f"BarrierIntakeCoordinator gained collector-shaped constructor parameter(s) "
            f"{collector_params!r} — integration has wired collector support into the "
            "classifier. _make_aggregation_only_coordinator must be updated to configure "
            "it (mirroring aggregation_executor/coalesce_executor here), and this test's "
            "orphan-raise expectation must be replaced with a positive-path collector-"
            "adoption assertion — it is no longer testing the fail-closed state."
        )

        row = _blocked_row(barrier_key=collector_barrier_key("stitch", "g-1"))
        scheduler = RecordingScheduler(pending=[row])
        coordinator = _make_aggregation_only_coordinator(scheduler=scheduler)

        with pytest.raises(AuditIntegrityError, match="orphan barrier_key"):
            coordinator.run_intake_pass(_ctx())

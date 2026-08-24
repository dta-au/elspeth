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
   whole-tree canary. I-2 (fix round) added a scan for CALLS to
   collector_barrier_key, since a helper call, however the arguments are
   computed, evades the ORIGINAL literal/f-string pattern match below. I-1
   (fix-round-3 review): that addition must not have REPLACED the literal
   scan — an inline f"collector:{name}:{group_id}" assigned to a
   barrier_key-named target is exactly what the original scan caught, and
   a helper-call-only scan misses it entirely. Both scans run; either
   shape trips the canary.
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
    reasoning below: only ast.Name.id (bare `collector_barrier_key(...)`)
    and ast.Attribute.attr (`work_items.collector_barrier_key(...)`) are
    read, each after the node's own type confirms the field exists.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_collector_prefixed_string(node: ast.expr) -> bool:
    """True for a literal or f-string node whose value starts with 'collector:'."""
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("collector:")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("collector:")
    return False


def _assignment_target_name(target: ast.expr) -> str | None:
    """The bound name for a simple or attribute assignment target, else None.

    Structural narrowing (isinstance over ast.expr's closed subclass set),
    not a dynamic-attribute probe: ast.Name.id and ast.Attribute.attr are
    each accessed directly only after the node's own type confirms the
    field exists. Subscript/Tuple/List/Starred targets (none of which are a
    plausible "variable literally named barrier_key" shape for this
    search's purpose) fall through to None rather than being probed.
    """
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def test_no_production_writer_of_collector_barrier_keys_exists_yet() -> None:
    """Scans every src/elspeth/**/*.py for EITHER shape of a collector
    barrier_key construction: a CALL to collector_barrier_key (I-2, catches
    a helper call however its arguments are computed), OR a barrier_key=
    call-keyword / assignment built from a "collector:"-prefixed literal or
    f-string (the ORIGINAL scan shape, restored ADDITIVELY per I-1,
    fix-round-3 review — the call-site scan alone missed an inline
    f"collector:{name}:{group_id}" assigned straight to a barrier_key-named
    target, which is exactly the shape the original scan existed to catch).
    The literal scan is deliberately narrow — core/dag/builder.py:723
    legitimately builds an unrelated schema-error owner label
    (f"collector:{collector_name}", not a barrier_key) that a naive
    whole-string scan would false-positive on; only assignments/kwargs
    literally named barrier_key count.
    """
    hits: list[tuple[str, int]] = []
    for path in sorted((REPO_ROOT / "src" / "elspeth").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if _call_target_name(node.func) == _HELPER_NAME:
                    hits.append((str(path.relative_to(REPO_ROOT)), node.lineno))
                    continue
                for kw in node.keywords:
                    if kw.arg == "barrier_key" and _is_collector_prefixed_string(kw.value):
                        hits.append((str(path.relative_to(REPO_ROOT)), node.lineno))
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is not None and _is_collector_prefixed_string(value):
                    for target in targets:
                        if _assignment_target_name(target) == "barrier_key":
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

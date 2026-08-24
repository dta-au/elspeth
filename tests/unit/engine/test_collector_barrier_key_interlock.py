"""WS4 Task 6 / META-14.2's collector barrier_key interlock — CONVERTED by the
WS3+WS4 integration (item 15) when the first production writer landed.

``collector_barrier_key(name, group_id)`` (core/landscape/scheduler/
work_items.py) is THE single construction site for the compound
"collector:<collector_name>:<group_id>" address (spec §4.3). Before
integration this file pinned that NO production writer existed and that
``BarrierIntakeCoordinator``'s classifier refused such a row as an orphan.
Both halves flipped in the same commit as the writer, per the self-destruct
note the original tests carried:

1. test_every_production_collector_barrier_key_goes_through_the_helper —
   the no-writer canary's successor. Same two AST walks (a CALL to the helper
   by name; a barrier_key= keyword / assignment built from a "collector:"
   literal or f-string), but the call walk now pins the EXACT set of
   production call sites and the literal walk stays a zero-tolerance
   canary. A new writer must either go through the helper AND be
   adjudicated into the expected set, or trip the literal walk — never
   hand-build the address silently. Residual blind spots (concat,
   ``.format()``, subscript targets) are closed by convention, not by this
   scan; ``git grep`` the fragments if you suspect one.
2. TestCollectorBarrierKeyIntake — the orphan-raise expectation's successor:
   a collector BLOCKED row classifies to the collector arm (fenced adoption
   with no batch membership, then ``CollectorExecutor.accept`` fed the
   intake ``ctx``), and the fail-closed twin: a row whose cursor and
   compound key disagree is refused as journal corruption.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from elspeth.contracts import TokenInfo
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.types import CollectorName, NodeID
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.engine.barrier_coordination import BarrierIntakeCoordinator, BarrierIntakeDispositionKind
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.collector import CollectorOutcome
from elspeth.engine.work_items import WorkItemFactory
from tests.unit.engine.test_barrier_coordination import (
    FakeNav,
    RecordingAggregationExecutor,
    RecordingScheduler,
    _batch_aware_transform,
    _blocked_row,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
_AGG_NODE = NodeID("agg-node")
_HELPER_NAME = collector_barrier_key.__name__

# The adjudicated production call sites of the helper: (path, enclosing
# function). Adding one is a deliberate edit here — the durable address has
# exactly these producers/consumers and nowhere else.
EXPECTED_HELPER_CALL_SITES = frozenset(
    {
        ("src/elspeth/engine/processor.py", "_maybe_collector_token"),
        ("src/elspeth/engine/processor.py", "_barrier_key_for_blocked_item"),
        ("src/elspeth/engine/processor.py", "_complete_collector_fire"),
        ("src/elspeth/engine/barrier_coordination.py", "_adopt_collector_row"),
        ("src/elspeth/engine/barrier_coordination.py", "_dispose_collector_outcome"),
        ("src/elspeth/engine/barrier_coordination.py", "restore_from_journal"),
    }
)


def _call_target_name(func: ast.expr) -> str | None:
    """The called symbol's bare name for a Name or Attribute call target, else None.

    Structural narrowing (isinstance over ast.expr's closed subclass set),
    not a dynamic-attribute probe: only ast.Name.id and ast.Attribute.attr
    are read, each after the node's own type confirms the field exists.
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
    """The bound name for a simple or attribute assignment target, else None."""
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _enclosing_function_name(tree: ast.AST, node: ast.AST) -> str:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    cursor: ast.AST | None = node
    while cursor is not None and not isinstance(cursor, ast.FunctionDef | ast.AsyncFunctionDef):
        cursor = parents.get(cursor)
    return cursor.name if isinstance(cursor, ast.FunctionDef | ast.AsyncFunctionDef) else "<module>"


def test_every_production_collector_barrier_key_goes_through_the_helper() -> None:
    """The helper's production call sites are exactly the adjudicated set,
    and NO production code builds a collector barrier_key from a literal.
    The literal walk is deliberately narrow — core/dag/builder.py builds an
    unrelated schema-error owner label (f"collector:{collector_name}", not a
    barrier_key) that a naive whole-string scan would false-positive on;
    only assignments/kwargs literally named barrier_key count."""
    helper_calls: set[tuple[str, str]] = set()
    literal_hits: list[tuple[str, int]] = []
    for path in sorted((REPO_ROOT / "src" / "elspeth").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        relative = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if _call_target_name(node.func) == _HELPER_NAME:
                    helper_calls.add((relative, _enclosing_function_name(tree, node)))
                    continue
                for kw in node.keywords:
                    if kw.arg == "barrier_key" and _is_collector_prefixed_string(kw.value):
                        literal_hits.append((relative, node.lineno))
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is not None and _is_collector_prefixed_string(value):
                    for target in targets:
                        if _assignment_target_name(target) == "barrier_key":
                            literal_hits.append((relative, node.lineno))
    assert literal_hits == [], (
        f"Production code hand-builds a collector barrier_key ({literal_hits!r}); "
        f"call {_HELPER_NAME} (core/landscape/scheduler/work_items.py) instead — it is the single construction site."
    )
    assert helper_calls == EXPECTED_HELPER_CALL_SITES, (
        f"{_HELPER_NAME} call sites drifted from the adjudicated set: "
        f"added={sorted(helper_calls - EXPECTED_HELPER_CALL_SITES)!r}, "
        f"removed={sorted(EXPECTED_HELPER_CALL_SITES - helper_calls)!r}. "
        "Update EXPECTED_HELPER_CALL_SITES deliberately."
    )


class RecordingCollectorExecutor:
    """Collector executor double: records accept() calls, returns a fixed outcome."""

    def __init__(self, outcome: CollectorOutcome) -> None:
        self.outcome = outcome
        self.accepted: list[tuple[str, str, object, float]] = []

    def accept(self, token: TokenInfo, collector_name: str, ctx: object, *, arrival_time: float) -> CollectorOutcome:
        self.accepted.append((token.token_id, collector_name, ctx, arrival_time))
        return self.outcome

    def notify_member_lost(self, *args: object, **kwargs: object) -> CollectorOutcome | None:
        raise AssertionError("not reached")


def _collector_blocked_row(*, collector_name: str = "stitch", group_id: str = "g-1", barrier_key: str | None = None) -> TokenWorkItem:
    row = _blocked_row(barrier_key=barrier_key if barrier_key is not None else collector_barrier_key(collector_name, group_id))
    return replace(
        row,
        lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id=group_id, member_key="tok-1"),),
        collector_name=collector_name,
    )


def _make_collector_coordinator(
    *,
    scheduler: RecordingScheduler,
    collector_executor: RecordingCollectorExecutor,
) -> BarrierIntakeCoordinator:
    """Minimal local construction: aggregation-only plus the collector plane."""
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
        collector_executor=collector_executor,  # type: ignore[arg-type]
        collector_node_ids={CollectorName("stitch"): NodeID("collector-node")},
        complete_collector_fire=lambda **kwargs: (_ for _ in ()).throw(AssertionError("not reached")),
        route_collector_release=lambda **kwargs: (_ for _ in ()).throw(AssertionError("not reached")),
    )


class TestCollectorBarrierKeyIntake:
    def test_collector_blocked_row_is_adopted_by_the_collector_arm(self) -> None:
        row = _collector_blocked_row()
        scheduler = RecordingScheduler(pending=[row])
        executor = RecordingCollectorExecutor(CollectorOutcome(held=True, collector_name="stitch", group_id="g-1"))
        coordinator = _make_collector_coordinator(scheduler=scheduler, collector_executor=executor)
        ctx = PluginContext(run_id="run-1", config={}, landscape=None)

        outcome = coordinator.run_intake_pass(ctx)

        assert scheduler.calls == ["list_pending", "adopt"]
        assert [(token_id, name) for token_id, name, _ctx, _arrival in executor.accepted] == [("tok-1", "stitch")]
        # The executor is fed the intake's OWN PluginContext, never one it constructs.
        assert executor.accepted[0][2] is ctx
        assert [d.kind for d in outcome.dispositions] == [BarrierIntakeDispositionKind.HELD]

    def test_adoption_skip_does_not_feed_executor_memory(self) -> None:
        row = _collector_blocked_row()
        scheduler = RecordingScheduler(pending=[row], adopted=False)
        executor = RecordingCollectorExecutor(CollectorOutcome(held=True, collector_name="stitch", group_id="g-1"))
        coordinator = _make_collector_coordinator(scheduler=scheduler, collector_executor=executor)

        outcome = coordinator.run_intake_pass(PluginContext(run_id="run-1", config={}, landscape=None))

        assert scheduler.calls == ["list_pending", "adopt"]
        assert executor.accepted == []
        assert outcome.dispositions == ()

    def test_cursor_and_compound_key_disagreement_fails_closed(self) -> None:
        row = _collector_blocked_row(barrier_key="stitch")
        scheduler = RecordingScheduler(pending=[row])
        executor = RecordingCollectorExecutor(CollectorOutcome(held=True, collector_name="stitch", group_id="g-1"))
        coordinator = _make_collector_coordinator(scheduler=scheduler, collector_executor=executor)

        with pytest.raises(AuditIntegrityError, match="journal corruption"):
            coordinator.run_intake_pass(PluginContext(run_id="run-1", config={}, landscape=None))
        assert scheduler.calls == ["list_pending"]

    def test_unconfigured_collector_cursor_is_still_an_orphan(self) -> None:
        row = _collector_blocked_row(collector_name="ghost")
        scheduler = RecordingScheduler(pending=[row])
        executor = RecordingCollectorExecutor(CollectorOutcome(held=True, collector_name="stitch", group_id="g-1"))
        coordinator = _make_collector_coordinator(scheduler=scheduler, collector_executor=executor)

        with pytest.raises(AuditIntegrityError, match="orphan barrier_key"):
            coordinator.run_intake_pass(PluginContext(run_id="run-1", config={}, landscape=None))

"""Spec §6.1: ONE settle-member routine — walk the failing token's
lineage_path from the innermost frame to the FIRST BOUND frame, stage one
GroupLossSpec for that frame's member. Record-then-notify: staging is
unconditional and precedes any in-memory notify; followers stage the
innermost bound loss only (notify is leader-only).

Built on tests/unit/engine/test_processor's `_make_processor` (WS3 Task 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.results import TransformResult
from elspeth.contracts.types import CoalesceName, NodeID
from elspeth.core.config import AggregationSettings
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding
from elspeth.testing import make_row, make_token_info
from tests.unit.engine.test_processor import (
    _FlushContext,
    _make_claimed_work_item,
    _make_contract,
    _make_factory,
    _make_mock_transform,
    _make_processor,
)

# ---------------------------------------------------------------------------
# Stub registry + fixture helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubGroupBindingRegistry:
    """Test double for GroupBindingRegistry (spec §3).

    `binding_for` is keyed directly on (frame.group_id, frame.member_key) —
    simpler than the real registry's FORK-by-member_key/EXPAND-by-group_id
    split, and sufficient here: `_settle_member_losses` calls only
    `binding_for`, nothing else in this suite touches the registry surface.
    Frames absent from `bindings` are inert (spec §2), matching the real
    registry's `None` return for an unregistered frame.
    """

    bindings: dict[tuple[str, str], GroupBinding]

    def binding_for(self, frame: LineageFrame) -> GroupBinding | None:
        return self.bindings.get((frame.group_id, frame.member_key))

    def by_opener_node(self) -> dict[NodeID, GroupBinding]:
        # TokenManager.__init__ always calls this when group_bindings is not
        # None (spec §4.2 mint-path wiring) — this suite never exercises
        # expand_token's registration, so an empty index is sufficient.
        return {}


def coalesce_binding(name: str, *, member_key: str = "path_a") -> GroupBinding:
    """A real GroupBinding (spec §3) naming `name` as a COALESCE closer."""
    return GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID(f"__synth_opener__{name}"),
        opener_name=f"__synth_opener__{name}",
        closer_node_id=NodeID(f"coalesce::{name}"),
        closer_name=name,
        closer_kind=CloserKind.COALESCE,
        policy="require_all",
        on_group_failure=None,
        member_roster=(member_key,),
    )


class _RecordingCoalesceExecutor:
    """Minimal `notify_branch_lost(coalesce_name, row_id, lost_branch, reason)`
    stand-in that records every call — no CoalesceOutcome, so the seam's
    leader-notify path returns [] after recording (outcome is None)."""

    def __init__(self) -> None:
        self.notified: list[tuple[str, str, str, str]] = []

    def notify_branch_lost(self, *, coalesce_name: Any, row_id: Any, lost_branch: Any, reason: Any) -> None:
        self.notified.append((str(coalesce_name), str(row_id), str(lost_branch), str(reason)))
        return None


@pytest.fixture
def recording_coalesce_executor() -> _RecordingCoalesceExecutor:
    return _RecordingCoalesceExecutor()


def make_token(*, lineage_path: tuple[LineageFrame, ...] = (), row_id: str = "row-1", token_id: str | None = None):
    return make_token_info(row_id=row_id, token_id=token_id or "tok-1", lineage_path=lineage_path)


def _coalesce_node_ids_for(bindings: dict[tuple[str, str], GroupBinding]) -> dict[CoalesceName, NodeID]:
    return {CoalesceName(b.closer_name): b.closer_node_id for b in bindings.values() if b.closer_kind is CloserKind.COALESCE}


@pytest.fixture
def processor_with_bindings():
    def _build(bindings: dict[tuple[str, str], GroupBinding], *, coalesce_executor: Any = None):
        _db, factory = _make_factory()
        registry = _StubGroupBindingRegistry(bindings=dict(bindings))
        return _make_processor(
            factory,
            group_bindings=registry,
            coalesce_executor=coalesce_executor,
            coalesce_node_ids=_coalesce_node_ids_for(bindings),
        )

    return _build


@pytest.fixture
def aggregation_flush_processor():
    def _build(
        *,
        quarantined_indices: set[int],
        member_paths: list[tuple[LineageFrame, ...]],
        bindings: dict[tuple[str, str], GroupBinding],
        coalesce_executor: Any = None,
    ):
        _db, factory = _make_factory()
        registry = _StubGroupBindingRegistry(bindings=dict(bindings))
        coalesce_node_ids = _coalesce_node_ids_for(bindings)
        processor = _make_processor(
            factory,
            group_bindings=registry,
            coalesce_executor=coalesce_executor,
            coalesce_node_ids=coalesce_node_ids,
            node_to_next={NodeID("agg-1"): None},
        )
        tokens = tuple(make_token_info(row_id=f"row-{i}", token_id=f"tok-{i}", lineage_path=path) for i, path in enumerate(member_paths))
        transform = _make_mock_transform(node_id="agg-1", name="agg_transform", creates_tokens=True)
        settings = AggregationSettings(
            name="agg",
            plugin="agg-transform",
            input="default",
            on_error="discard",
            trigger={"count": len(tokens)},
            output_mode="transform",
        )
        fctx = _FlushContext(
            node_id=NodeID("agg-1"),
            transform=transform,
            settings=settings,
            buffered_tokens=tokens,
            batch_id="batch-1",
            error_msg="batch flush failed",
            expand_parent_token=tokens[0],
            triggering_token=None,
            coalesce_node_id=None,
            coalesce_name=None,
        )
        flush_result = TransformResult.success(
            make_row({"value": 1}, contract=_make_contract()),
            success_reason={"action": "batch_processed", "metadata": {"quarantined_indices": sorted(quarantined_indices)}},
        )

        def _flush():
            with patch.object(processor._token_manager, "expand_token", return_value=([], "expand-group-1")):
                return processor._route_transform_results(fctx, flush_result)

        return processor, _flush

    return _build


# ---------------------------------------------------------------------------
# Fixed lineage frames used across the walk tests
# ---------------------------------------------------------------------------

INNER_FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg_inner", member_key="path_a")
OUTER_FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg_outer", member_key="left")
INERT_EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg_inert", member_key="tok_c1")


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def test_walk_stages_loss_for_first_bound_frame_skipping_inert(processor_with_bindings) -> None:
    """Innermost-to-first-BOUND: an inert innermost frame is skipped, the
    first BOUND frame gets the one staged loss. Kills the
    walk-stops-at-innermost mutant."""
    proc = processor_with_bindings({("fg_inner", "path_a"): coalesce_binding("merge_inner")})
    token = make_token(lineage_path=(OUTER_FORK, INNER_FORK, INERT_EXPAND))
    proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False)
    (spec,) = proc._pending_group_losses
    assert (spec.closer_name, spec.group_id, spec.member_key) == ("merge_inner", "fg_inner", "path_a")


def test_walk_is_innermost_first_not_outermost_first(processor_with_bindings) -> None:
    """BOTH frames bound: the INNER one is settled. Kills the
    outermost-first mutant."""
    proc = processor_with_bindings(
        {
            ("fg_inner", "path_a"): coalesce_binding("merge_inner"),
            ("fg_outer", "left"): coalesce_binding("merge_outer", member_key="left"),
        }
    )
    token = make_token(lineage_path=(OUTER_FORK, INNER_FORK))
    proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False)
    (spec,) = proc._pending_group_losses
    assert spec.group_id == "fg_inner"


def test_all_inert_path_stages_nothing(processor_with_bindings) -> None:
    """Unbound frames are inert provenance (§2): nobody waits, nothing is
    staged — the batch posture, structurally."""
    proc = processor_with_bindings({})
    token = make_token(lineage_path=(OUTER_FORK, INERT_EXPAND))
    assert proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False) == []
    assert proc._pending_group_losses == []


def test_root_token_settles_nothing(processor_with_bindings) -> None:
    proc = processor_with_bindings({})
    token = make_token(lineage_path=())
    assert proc._settle_member_losses(token, "quarantined", []) == []


def test_staging_precedes_notify_and_survives_notify_absence(processor_with_bindings) -> None:
    """Record-then-notify (processor.py discipline, carried): the durable
    staging happens even when this worker has no executor (follower)."""
    proc = processor_with_bindings({("fg_inner", "path_a"): coalesce_binding("merge_inner")}, coalesce_executor=None)
    token = make_token(lineage_path=(INNER_FORK,))
    assert proc._settle_member_losses(token, "quarantined", []) == []
    assert len(proc._pending_group_losses) == 1


def test_leader_notify_dispatches_to_coalesce_executor(processor_with_bindings, recording_coalesce_executor) -> None:
    proc = processor_with_bindings(
        {("fg_inner", "path_a"): coalesce_binding("merge_inner")},
        coalesce_executor=recording_coalesce_executor,
    )
    token = make_token(lineage_path=(INNER_FORK,), row_id="row-1")
    proc._settle_member_losses(token, "quarantined", [])
    assert recording_coalesce_executor.notified == [("merge_inner", "row-1", "path_a", "quarantined")]


def test_quarantined_batch_member_with_a_bound_frame_fails_fast_not_staged(aggregation_flush_processor) -> None:
    """Bypass site 2 (spec §6.1 item 2), corrected per review I1
    (2026-08-24): a quarantined batch member carrying a BOUND frame is a
    ruling-25 violation (aggregators are banned inside every bound region,
    enforced at build time — bound_regions.py::validate_no_aggregations_in_regions),
    unreachable in a buildable graph. `_route_transform_results` has no
    consumer for `_pending_group_losses` the way the empty-flush path's
    `complete_barrier` does — staging a spec here would orphan it until some
    LATER, unrelated claim's guard trips on it. The non-empty flush therefore
    fails FAST at this site instead of calling the settle-member seam,
    naming ruling 25 in the message, rather than staging a loss this path
    cannot commit."""
    proc, flush = aggregation_flush_processor(
        quarantined_indices={0},
        member_paths=[(INNER_FORK,), ()],
        bindings={("fg_inner", "path_a"): coalesce_binding("merge_inner")},
    )
    with pytest.raises(OrchestrationInvariantError, match="Ruling 25"):
        flush()
    assert proc._pending_group_losses == []


def test_quarantined_batch_member_with_an_unbound_frame_is_a_structural_noop(aggregation_flush_processor) -> None:
    """The common case: no binding registered at all (matches ruling 25's
    actual production shape — an aggregation's buffered tokens are never
    inside a bound region) — the flush proceeds normally, nothing staged,
    nothing raised."""
    proc, flush = aggregation_flush_processor(
        quarantined_indices={0},
        member_paths=[(INNER_FORK,), ()],
        bindings={},
    )
    flush()
    assert proc._pending_group_losses == []


def test_stage_group_loss_rejects_a_second_loss_for_the_same_bound_frame(processor_with_bindings) -> None:
    """Two tokens sharing the same bound frame staged within one claim (spec
    §6.1: at most one loss per bound frame per claim) is a processor bug.
    Ruling 25 bans aggregators inside bound regions, so two BUFFERED members
    of one aggregation flush sharing a bound frame should be unreachable via
    a real, ruling-25-compliant graph — but `_stage_group_loss` cannot see
    that invariant (it is a build-time graph-validation concern, not this
    runtime seam's job), so it fails closed rather than silently dropping
    or double-counting a loss. Pin the guard fires through the REAL seam,
    not just the drain's separate claim-guard
    (test_group_loss_claim_guard.py's `test_guard_rejects_two_losses_for_one_frame`
    pins a different guard, at a different site, over hand-built specs)."""
    proc = processor_with_bindings({("fg_inner", "path_a"): coalesce_binding("merge_inner")})
    token_a = make_token(lineage_path=(INNER_FORK,), token_id="tok-a")
    token_b = make_token(lineage_path=(INNER_FORK,), token_id="tok-b")
    proc._settle_member_losses(token_a, "quarantined", [], notify_in_memory=False)
    with pytest.raises(OrchestrationInvariantError, match="at most one loss per bound frame"):
        proc._settle_member_losses(token_b, "quarantined", [], notify_in_memory=False)


def test_stage_group_loss_is_idempotent_for_an_identical_triple(processor_with_bindings) -> None:
    """Ruling 42's idempotent-triple tolerance, pinned directly at the seam
    (Ruling 44): staging the exact same (group_id, member_key, token_id)
    triple twice — e.g. two independent observers noticing the identical
    fact through different callers — is a duplicate OBSERVATION, not
    corruption, and is a no-op rather than tripping the cross-token-id raise
    pinned immediately above. Previously covered only end-to-end
    (test_coalesce_sweep_escalation_durability.py); this was the reviewer's
    residual "no direct unit coverage" note."""
    proc = processor_with_bindings({("fg_inner", "path_a"): coalesce_binding("merge_inner")})
    token = make_token(lineage_path=(INNER_FORK,), token_id="tok-a")
    proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False)
    proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False)
    assert len(proc._pending_group_losses) == 1


def test_record_group_member_terminals_settles_once_not_per_consumed_token(processor_with_bindings) -> None:
    """Ruling 38 / C1's fix, pinned directly (Ruling 44 / R1). Consumed
    siblings share their enclosing frame by construction (that is what
    makes them siblings), so `_record_group_member_terminals` must run the
    escalation walk ONCE per call, not once per consumed token — a
    per-token loop stages the same (group_id, member_key) N times.

    This can no longer be pinned by observing the DURABLE outcome: Ruling
    42 taught `_stage_group_loss` to treat a repeated identical
    (group_id, member_key, token_id) triple as an idempotent no-op, so a
    regressed per-token loop now produces the exact same final staged/
    committed state as the fix — both
    `test_coalesce_sweep_escalation_durability.py` tests would stay green
    against it. Counting the walk invocation directly closes that gap:
    it reds on a per-token-loop regression even though idempotency masks
    the durable outcome (verified via a throwaway revert of the dedupe,
    per the project's committed-pin A/B discipline)."""
    proc = processor_with_bindings({("fg_outer", "left"): coalesce_binding("merge_outer", member_key="left")})
    token_a = make_token(lineage_path=(OUTER_FORK, INNER_FORK), token_id="tok-a")
    token_b = make_token(lineage_path=(OUTER_FORK, INNER_FORK), token_id="tok-b")

    with (
        patch.object(proc, "_settle_member_losses", return_value=[]) as mock_settle,
        patch.object(proc._data_flow, "record_token_outcome") as mock_record_token_outcome,
    ):
        proc._record_group_member_terminals(consumed_tokens=(token_a, token_b), failure_reason="quarantined", child_items=[])

    assert mock_settle.call_count == 1
    (remaining_token, reason, child_items), kwargs = mock_settle.call_args
    assert remaining_token.token_id == "tok-a"
    assert remaining_token.lineage_path == (OUTER_FORK,)
    assert reason == "quarantined"
    assert child_items == []
    assert kwargs == {"escalated": True}
    # Every consumed token still gets its own terminal write even though the
    # walk itself is shared — the dedupe is scoped to the escalation walk,
    # not the per-token terminal record.
    assert mock_record_token_outcome.call_count == 2


# ---------------------------------------------------------------------------
# Collector arm: WS2 forbids building collector bindings until WS4's
# executor registers, so this is unreachable in a built pipeline today — the
# raise keeps it fail-closed rather than silently staged-but-never-notified.
# ---------------------------------------------------------------------------


def test_collector_closer_raises_fail_closed(processor_with_bindings) -> None:
    collector_binding = GroupBinding(
        kind=FrameKind.EXPAND,
        opener_node_id=NodeID("__synth_opener__scope"),
        opener_name="__synth_opener__scope",
        closer_node_id=NodeID("collector::scope"),
        closer_name="scope_collector",
        closer_kind=CloserKind.COLLECTOR,
        policy="require_all",
        on_group_failure=None,
        member_roster=(),
    )
    proc = processor_with_bindings({("eg_scope", "tok-child"): collector_binding})
    token = make_token(lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="eg_scope", member_key="tok-child"),))
    with pytest.raises(OrchestrationInvariantError, match="collector settlement lands in WS4"):
        proc._settle_member_losses(token, "quarantined", [])


# ---------------------------------------------------------------------------
# Claim-guard tying (WS3 Task 5 review requirement): the claimed work item's
# lineage_path must match the in-memory current_token.lineage_path at
# staging time — stage via the REAL _settle_member_losses path (unlike
# test_group_loss_claim_guard.py's isolated guard tests, which hand-build
# GroupLossSpec directly) and take via the real claim guard.
# ---------------------------------------------------------------------------


def test_claim_guard_accepts_the_settled_frame(processor_with_bindings) -> None:
    proc = processor_with_bindings({("fg_inner", "path_a"): coalesce_binding("merge_inner")})
    token = make_token(lineage_path=(INNER_FORK,), token_id="tok-settled")
    proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False)

    claimed = _make_claimed_work_item(token_id="tok-settled", lineage_path=token.lineage_path)
    losses = proc._take_claim_group_losses(claimed)
    assert len(losses) == 1
    assert losses[0].member_key == "path_a"


def test_claim_guard_rejects_a_mismatched_lineage_for_the_same_settled_loss(processor_with_bindings) -> None:
    proc = processor_with_bindings({("fg_inner", "path_a"): coalesce_binding("merge_inner")})
    token = make_token(lineage_path=(INNER_FORK,), token_id="tok-settled")
    proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False)

    mismatched = _make_claimed_work_item(token_id="tok-settled", lineage_path=(OUTER_FORK,))
    with pytest.raises(OrchestrationInvariantError, match="lineage path"):
        proc._take_claim_group_losses(mismatched)


# ---------------------------------------------------------------------------
# Registry identity (WS3 Task 5 review requirement): register_expand_group
# mutates ONE registry instance; binding_for reads it. A copy anywhere on
# the graph -> RowProcessor -> TokenManager wiring path would make expand
# registration permanently invisible to the walk, with every stub-based
# test above still green.
# ---------------------------------------------------------------------------


def test_token_manager_shares_the_processors_group_bindings_instance(processor_with_bindings) -> None:
    proc = processor_with_bindings({("fg_inner", "path_a"): coalesce_binding("merge_inner")})
    assert proc._token_manager._group_bindings is proc._group_bindings


def test_group_bindings_registry_identity_survives_processor_factory_wiring() -> None:
    """Exercise the REAL production wiring
    (processor_factory.build_row_processor), not a stub: graph.get_group_bindings()
    must be the SAME object RowProcessor and its TokenManager hold."""
    from elspeth.core.config import SourceSettings
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.orchestrator import PipelineConfig
    from elspeth.engine.orchestrator.processor_factory import build_row_processor
    from elspeth.engine.spans import SpanFactory
    from tests.fixtures.base_classes import as_sink, as_source
    from tests.fixtures.landscape import make_recorder_with_run
    from tests.fixtures.plugins import CollectSink, ListSource
    from tests.fixtures.stores import MockPayloadStore

    source = ListSource([{"value": 1}], name="source")
    sink = CollectSink("default")
    source_settings = SourceSettings(plugin=source.name, on_success="default", options={})

    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(source)},
        source_settings_map={"primary": source_settings},
        transforms=[],
        sinks={"default": as_sink(sink)},
        aggregations={},
        gates=[],
    )

    setup = make_recorder_with_run(source_node_id=str(graph.get_sources()[0]))
    config = PipelineConfig(sources={"primary": as_source(source)}, transforms=[], sinks={"default": as_sink(sink)})

    processor, _coalesce_map, _coalesce_executor = build_row_processor(
        graph=graph,
        config=config,
        settings=None,
        factory=setup.factory,
        run_id=setup.run_id,
        source_id=graph.get_sources()[0],
        edge_map={},
        route_resolution_map=None,
        config_gate_id_map={},
        coalesce_id_map={},
        payload_store=MockPayloadStore(),
        span_factory=SpanFactory(),
        clock=None,
        max_workers=None,
        telemetry=None,
    )

    registry = graph.get_group_bindings()
    assert processor._group_bindings is registry
    assert processor._token_manager._group_bindings is registry

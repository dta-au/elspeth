"""ADR-019 resume aggregation parity for phase-3 predicate counters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from elspeth.contracts import NodeStateStatus, NodeType
from elspeth.contracts.errors import CoalesceFailureReason
from elspeth.contracts.run_result import RunResult
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.orchestrator.run_status import derive_resume_terminal_status_from_audit
from tests.fixtures.base_classes import _TestSourceBase
from tests.fixtures.landscape import make_recorder_with_run, register_test_node
from tests.fixtures.plugins import ListSource as _ListSource
from tests.fixtures.stores import MockClock
from tests.integration._helpers import (
    build_test_pipeline_with_discard_sink,
    build_test_pipeline_with_failsink_diversion,
    build_test_pipeline_with_gate_route,
    build_test_pipeline_with_on_error_route,
    build_test_pipeline_with_source_quarantine,
    run_pipeline,
)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    result: RunResult
    db: LandscapeDB
    run_id: str


def _counter_snapshot(result: RunResult) -> dict[str, object]:
    return {
        "status": result.status,
        "rows_processed": result.rows_processed,
        "rows_succeeded": result.rows_succeeded,
        "rows_failed": result.rows_failed,
        "rows_routed_success": result.rows_routed_success,
        "rows_routed_failure": result.rows_routed_failure,
        "rows_quarantined": result.rows_quarantined,
        "rows_forked": result.rows_forked,
        "rows_coalesced": result.rows_coalesced,
        "rows_expanded": result.rows_expanded,
        "rows_buffered": result.rows_buffered,
        "rows_diverted": result.rows_diverted,
        "rows_coalesce_failed": result.rows_coalesce_failed,
        "routed_destinations": dict(result.routed_destinations),
    }


def _resume_counter_snapshot_from_audit(
    db: LandscapeDB,
    run_id: str,
) -> dict[str, object]:
    factory = RecorderFactory(db)
    status, counters = derive_resume_terminal_status_from_audit(factory, run_id)
    return {
        "status": status,
        "rows_processed": counters.rows_processed,
        "rows_succeeded": counters.rows_succeeded,
        "rows_failed": counters.rows_failed,
        "rows_routed_success": counters.rows_routed_success,
        "rows_routed_failure": counters.rows_routed_failure,
        "rows_quarantined": counters.rows_quarantined,
        "rows_forked": counters.rows_forked,
        "rows_coalesced": counters.rows_coalesced,
        "rows_expanded": counters.rows_expanded,
        "rows_buffered": counters.rows_buffered,
        "rows_diverted": counters.rows_diverted,
        "rows_coalesce_failed": counters.rows_coalesce_failed,
        "routed_destinations": dict(counters.routed_destinations),
    }


def run_live_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> ScenarioResult:
    if scenario == "gate_routed_success":
        config, graph, db, store = build_test_pipeline_with_gate_route(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            routed_row_count=3,
            default_flow_row_count=1,
        )
    elif scenario == "on_error_routed_failure":
        config, graph, db, store = build_test_pipeline_with_on_error_route(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            on_error_routed_count=2,
            success_count=1,
        )
    elif scenario == "quarantine_failure":
        config, graph, db, store = build_test_pipeline_with_source_quarantine(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            quarantine_row_count=2,
        )
    elif scenario == "failsink_mode_diversion":
        config, graph, db, store = build_test_pipeline_with_failsink_diversion(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            diverted_row_count=2,
            success_row_count=1,
        )
    elif scenario == "discard_mode_diversion":
        config, graph, db, store = build_test_pipeline_with_discard_sink(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            success_row_count=1,
            discard_row_count=2,
        )
    else:
        raise AssertionError(f"unknown scenario {scenario!r}")

    result = run_pipeline(config, graph, db, store)
    return ScenarioResult(result=result, db=db, run_id=result.run_id)


@pytest.mark.parametrize(
    "scenario",
    [
        "gate_routed_success",
        "on_error_routed_failure",
        "quarantine_failure",
        "failsink_mode_diversion",
        "discard_mode_diversion",
    ],
)
def test_resume_counter_shape_matches_live_predicate_counters(
    scenario: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit-derived resume counters match live status and RunResult counters."""
    live = run_live_scenario(tmp_path / scenario, monkeypatch, scenario)

    assert _resume_counter_snapshot_from_audit(live.db, live.run_id) == _counter_snapshot(live.result)


@pytest.mark.parametrize("scenario", ["failsink_mode_diversion", "discard_mode_diversion"])
def test_resume_counter_derivation_replays_diversion_structural_count(
    scenario: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural ``rows_diverted`` is replayed from audit in the all-terminal branch."""
    live = run_live_scenario(tmp_path / scenario, monkeypatch, scenario)
    factory = RecorderFactory(live.db)

    _status, counters = derive_resume_terminal_status_from_audit(factory, live.run_id)

    assert counters.rows_diverted == live.result.rows_diverted


# ─────────────────────────────────────────────────────────────────────────
# F2 (resume-fork-reemit): rows_processed is per SOURCE ROW, not per terminal
# token.  derive_resume_terminal_status_from_audit reconstructs it as the count
# of DISTINCT source row_id reaching a terminal outcome
# (AuditRunStatusProjection.count_distinct_source_rows_with_terminal_outcome), NOT a
# per-leaf tally.  Structural fan-out (fork / expand) and fan-in (aggregation)
# are the cases where the two diverge — the parametrized scenarios above are all
# 1-row-1-leaf and so cannot catch a per-leaf regression.  These archetype
# guards lock in the fix and protect BOTH resume branches (the helper is shared).
#
# The "old per-leaf value" in each docstring is what the pre-F2 logic produced
# (one rows_processed += 1 per SUCCESS/FAILURE terminal leaf); the assertions pin
# that it is no longer produced.
# ─────────────────────────────────────────────────────────────────────────


def _derive_rows_processed(db: object, run_id: str) -> int:
    factory = RecorderFactory(db)  # type: ignore[arg-type]
    _status, counters = derive_resume_terminal_status_from_audit(factory, run_id)
    return counters.rows_processed


def test_derive_rows_processed_fork_counts_source_rows_not_leaves() -> None:
    """Fork 1 source row -> 2 branches: rows_processed == 1 (source rows), not 2 (leaves).

    Pre-F2 per-leaf logic counted the two SUCCESS/DEFAULT_FLOW fork leaves and
    reported rows_processed == 2.  The fork children inherit the parent's single
    source row_id, so the faithful per-source-row count is 1.
    """
    from elspeth.core.config import GateSettings, SourceSettings
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
    from tests.fixtures.base_classes import as_sink, as_source
    from tests.fixtures.landscape import make_landscape_db
    from tests.fixtures.plugins import CollectSink, ListSource
    from tests.fixtures.stores import MockPayloadStore

    db = make_landscape_db()
    src = ListSource([{"value": 1}], on_success="sink_a")
    sink_a, sink_b = CollectSink("sink_a"), CollectSink("sink_b")
    gate = GateSettings(
        name="fork_gate",
        input="gate_in",
        condition="True",
        routes={"true": "fork", "false": "sink_a"},
        fork_to=["sink_a", "sink_b"],
    )
    config = PipelineConfig(
        sources={"primary": as_source(src)},
        transforms=[],
        sinks={"sink_a": as_sink(sink_a), "sink_b": as_sink(sink_b)},
        gates=[gate],
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(src)},
        source_settings_map={"primary": SourceSettings(plugin=src.name, on_success="gate_in", options={})},
        transforms=[],
        sinks={"sink_a": as_sink(sink_a), "sink_b": as_sink(sink_b)},
        gates=[gate],
        aggregations={},
        coalesce_settings=[],
    )
    run = Orchestrator(db).run(config, graph=graph, payload_store=MockPayloadStore())

    # Live truth: 1 source row, 2 success leaves, 1 fork parent.
    assert run.rows_processed == 1
    assert run.rows_succeeded == 2
    assert run.rows_forked == 1

    derived = _derive_rows_processed(db, run.run_id)
    assert derived == 1, f"fork: derive rows_processed must be source-row count 1, got {derived}"
    assert derived == run.rows_processed, "fork: derive must match uninterrupted run rows_processed"
    assert derived != run.rows_succeeded, "fork: derive must NOT be the per-leaf count (2) — that was the pre-F2 bug"


def test_derive_rows_processed_aggregation_counts_source_rows_not_result() -> None:
    """Aggregation 3 source rows -> 1 result: rows_processed == 3 (source rows), not 1 (result token).

    Pre-F2 per-leaf logic counted only the single SUCCESS/DEFAULT_FLOW result
    token (the 3 BATCH_CONSUMED tokens hit the no-op case) and reported
    rows_processed == 1.  The 3 BATCH_CONSUMED tokens retain their source
    row_ids, so the faithful per-source-row count is 3.
    """
    from typing import Any

    from elspeth.contracts.enums import Determinism, OutputMode
    from elspeth.contracts.schema_contract import PipelineRow
    from elspeth.core.config import AggregationSettings, SourceSettings, TriggerConfig
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
    from elspeth.plugins.infrastructure.base import BaseTransform
    from elspeth.plugins.infrastructure.results import TransformResult
    from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
    from tests.fixtures.landscape import make_landscape_db
    from tests.fixtures.plugins import CollectSink, ListSource
    from tests.fixtures.stores import MockPayloadStore

    class _SumAggregator(BaseTransform):
        name = "sum-aggregator"
        determinism = Determinism.DETERMINISTIC
        plugin_version = "1.0.0"
        source_file_hash = None
        input_schema = _TestSchema
        output_schema = _TestSchema
        is_batch_aware = True
        passes_through_input = False
        forwards_input_fields = False
        removed_input_fields = frozenset()
        on_success = "output"
        on_error = "discard"

        def __init__(self) -> None:
            super().__init__({"schema": {"mode": "observed"}})

        def process(self, rows: list[PipelineRow], ctx: Any) -> TransformResult:  # type: ignore[override]
            if not rows:
                return TransformResult.error({"reason": "empty"}, retryable=False)
            total = sum(r.to_dict().get("value", 0) for r in rows)
            return TransformResult.success(PipelineRow({"sum": total}, rows[0].contract), success_reason={"action": "sum"})

    n = 3
    db = make_landscape_db()
    src = ListSource([{"value": i + 1} for i in range(n)], name="list_source", on_success="agg_in")
    out = CollectSink("output")
    agg = _SumAggregator()
    agg_settings = AggregationSettings(
        name="sum_agg",
        plugin=agg.name,
        input="agg_in",
        on_success="output",
        on_error="discard",
        trigger=TriggerConfig(count=n, timeout_seconds=3600),
        output_mode=OutputMode.TRANSFORM,
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(src)},
        source_settings_map={"primary": SourceSettings(plugin=src.name, on_success="agg_in", options={})},
        transforms=[],
        sinks={"output": as_sink(out)},
        aggregations={"sum_agg": (as_transform(agg), agg_settings)},
        gates=[],
    )
    agg_id_map = graph.get_aggregation_id_map()
    agg_node_id = agg_id_map[next(iter(agg_id_map))]
    agg.node_id = agg_node_id
    config = PipelineConfig(
        sources={"primary": as_source(src)},
        transforms=[as_transform(agg)],
        sinks={"output": as_sink(out)},
        aggregation_settings={agg_node_id: agg_settings},
    )
    run = Orchestrator(db).run(config, graph=graph, payload_store=MockPayloadStore())

    # Live truth: 3 source rows, 1 aggregate result.
    assert run.rows_processed == n
    assert run.rows_succeeded == 1

    derived = _derive_rows_processed(db, run.run_id)
    assert derived == n, f"aggregation: derive rows_processed must be source-row count {n}, got {derived}"
    assert derived == run.rows_processed, "aggregation: derive must match uninterrupted run rows_processed"
    assert derived != run.rows_succeeded, "aggregation: derive must NOT be the result-token count (1) — that was the pre-F2 bug"


def test_derive_rows_processed_expand_counts_source_rows_not_children() -> None:
    """Expand 1 source row -> 3 children: rows_processed == 1 (source rows), not 3 (children).

    Pre-F2 per-leaf logic counted the three SUCCESS/DEFAULT_FLOW expand children
    and reported rows_processed == 3.  Expand children inherit the parent's
    single source row_id, so the faithful per-source-row count is 1.
    """
    from typing import Any

    from elspeth.contracts.enums import Determinism
    from elspeth.contracts.schema_contract import PipelineRow
    from elspeth.core.config import SourceSettings
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
    from elspeth.plugins.infrastructure.base import BaseTransform
    from elspeth.plugins.infrastructure.results import TransformResult
    from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
    from tests.fixtures.factories import wire_transforms
    from tests.fixtures.landscape import make_landscape_db
    from tests.fixtures.plugins import CollectSink, ListSource
    from tests.fixtures.stores import MockPayloadStore

    class _ExpandTransform(BaseTransform):
        name = "expand-transform"
        determinism = Determinism.DETERMINISTIC
        plugin_version = "1.0.0"
        source_file_hash = None
        input_schema = _TestSchema
        output_schema = _TestSchema
        is_batch_aware = False
        passes_through_input = False
        forwards_input_fields = False
        removed_input_fields = frozenset()
        creates_tokens = True
        on_success = "output"
        on_error = "discard"

        def __init__(self) -> None:
            super().__init__({"schema": {"mode": "observed"}})

        def process(self, row: PipelineRow, ctx: Any) -> TransformResult:  # type: ignore[override]
            c = row.contract
            return TransformResult.success_multi(
                (PipelineRow({"value": 10}, c), PipelineRow({"value": 20}, c), PipelineRow({"value": 30}, c)),
                success_reason={"action": "expand"},
            )

    db = make_landscape_db()
    src = ListSource([{"value": 1}], name="list_source", on_success="source_out")
    out = CollectSink("output")
    xf = _ExpandTransform()
    wired = wire_transforms([as_transform(xf)], source_connection="source_out", final_sink="output", names=["expand-transform"])
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(src)},
        source_settings_map={"primary": SourceSettings(plugin=src.name, on_success="source_out", options={})},
        transforms=wired,
        sinks={"output": as_sink(out)},
        aggregations={},
        gates=[],
    )
    config = PipelineConfig(sources={"primary": as_source(src)}, transforms=[as_transform(xf)], sinks={"output": as_sink(out)})
    run = Orchestrator(db).run(config, graph=graph, payload_store=MockPayloadStore())

    # Live truth: 1 source row, 3 expanded children, 1 expand parent.
    assert run.rows_processed == 1
    assert run.rows_succeeded == 3
    assert run.rows_expanded == 1

    derived = _derive_rows_processed(db, run.run_id)
    assert derived == 1, f"expand: derive rows_processed must be source-row count 1, got {derived}"
    assert derived == run.rows_processed, "expand: derive must match uninterrupted run rows_processed"
    assert derived != run.rows_succeeded, "expand: derive must NOT be the per-child count (3) — that was the pre-F2 bug"


# ─────────────────────────────────────────────────────────────────────────
# rows_coalesce_failed cumulative audit derive, end-to-end via the coalesce
# TIMEOUT path (elspeth-7294de558e — closes F2 review Item D).
#
# rows_coalesce_failed historically had NO audit arm: a failed coalesce emitted
# its operation roll-up to telemetry only, so the with-rows resume branch
# GRAFTED the value from the live re-drive counter — which only saw failures
# during THAT resume's re-drive and forgot run-1 failures consumed before the
# interrupt (resumed runs under-reported vs the uninterrupted oracle: 1 vs 3 in
# this topology). derive_resume_terminal_status_from_audit now reconstructs it
# from the durable FAILED node_states that CoalesceExecutor._fail_pending
# writes at the run's coalesce nodes, counted as DISTINCT (coalesce node,
# row_id) pairs (one failed BARRIER == one row, regardless of how many branch
# tokens it consumed — token_outcomes are per BRANCH and carry no node
# attribution, so they cannot be the anchor). The query is run-scoped and
# resume re-drives record under the SAME run_id, so the derived value covers
# run-1 AND resume failures: run_B == run_A, asserted as EQUALITY below.
#
# Constructing the timeout deterministically: CoalesceExecutor takes an
# injectable clock (Orchestrator(clock=...) → CoalesceExecutor._clock). A source
# that advances the injected MockClock BEFORE yielding each row makes a prior
# row's pending barrier (1-of-2 arrived; the agg branch is held buffered, never
# triggering its count=100 trigger) elapse past the coalesce timeout by the time
# the NEXT row's per-row handle_coalesce_timeouts runs.
#
# Constructing a RESUMABLE interrupt (multi-source branch): mid-source
# interrupts are no longer resumable (IncompleteSourceResumeError — resume
# refuses a non-exhausted source), so run_B interrupts on its FINAL source row
# (all rows processed, EOF flushes not yet run) and the interruption is
# reshaped into the exhausted-then-crashed-EOF-flush state exactly as
# tests/integration/pipeline/test_eof_resume_proof.py does:
# run_sources.lifecycle_state='exhausted' (what finalize_source_iteration
# records before the EOF flush) + runs.status='failed' (what the failure
# ceremony records when that flush crashes).
#
# REGRESSION-GUARD STRENGTH: run-1 consumes at least one timed-out barrier
# before the interrupt (asserted non-vacuously via the derive query pre-resume)
# and the resume consumes the rest at its EOF coalesce flush. A regression to
# resume-local counting (the old graft) drops run_B below run_A; a regression
# to per-branch-outcome counting over-reports a multi-branch barrier. Both go
# RED on the equality pin.
# ─────────────────────────────────────────────────────────────────────────


class _ClockAdvancingCoalesceSource(_TestSourceBase):
    """Source that advances an injected MockClock before each yield (so prior
    pending coalesce barriers elapse past the timeout at the next per-row check)
    and optionally sets a shutdown event after ``interrupt_after`` rows.

    Subclasses _TestSourceBase so both run A (uninterrupted) and run B
    (interrupt + resume) build identical topology through production
    ExecutionGraph.from_plugin_instances / Orchestrator paths.
    """

    name = "f2_clock_advancing_coalesce_source"
    output_schema = _ListSource.output_schema

    def __init__(self, rows, clock, step, shutdown_event=None, interrupt_after=None):
        super().__init__()
        self._rows = rows
        self._clock = clock
        self._step = step
        self._event = shutdown_event
        self._interrupt_after = interrupt_after
        self.on_success = "fork_input"

    def on_start(self, ctx):  # pragma: no cover - trivial
        pass

    def close(self):  # pragma: no cover - trivial
        pass

    def load(self, ctx):
        from elspeth.contracts.results import SourceRow
        from elspeth.contracts.schema_contract import FieldContract, SchemaContract

        for index, row in enumerate(self._rows, start=1):
            # Advance BEFORE yield: any barrier created by a prior row now reads
            # elapsed >= timeout at this row's post-processing handle_coalesce_timeouts.
            self._clock.advance(self._step)
            if self._event is not None and self._interrupt_after is not None and index >= self._interrupt_after:
                self._event.set()
            fields = tuple(
                FieldContract(normalized_name=k, original_name=k, python_type=object, required=False, source="inferred") for k in row
            )
            contract = SchemaContract(mode="OBSERVED", fields=fields, locked=True)
            self._schema_contract = contract
            yield SourceRow.valid(row, contract=contract, source_row_index=index - 1)


def _build_quorum_timeout_coalesce(clock, rows, *, shutdown_event=None, interrupt_after=None):
    """fork → (buffered agg branch) + (direct branch) → quorum(2) coalesce with a
    timeout. The agg branch holds its token (count=100 trigger never fires), so
    only the direct branch arrives → the barrier times out as quorum_not_met."""
    from elspeth.contracts.types import AggregationName
    from elspeth.core.config import (
        AggregationSettings,
        CoalesceSettings,
        ElspethSettings,
        GateSettings,
        SourceSettings,
        TriggerConfig,
    )
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.orchestrator import PipelineConfig
    from tests.fixtures.base_classes import as_sink, as_source, as_transform
    from tests.fixtures.plugins import CollectSink
    from tests.integration.pipeline.orchestrator.test_graceful_shutdown import InterruptAfterNBufferedBatch

    source = _ClockAdvancingCoalesceSource(rows, clock, step=100.0, shutdown_event=shutdown_event, interrupt_after=interrupt_after)
    output_sink = CollectSink("output")
    batch_transform = InterruptAfterNBufferedBatch()
    batch_transform.on_success = "agg_ready"
    batch_transform.on_error = "discard"

    fork_gate = GateSettings(
        name="fork_gate",
        input="fork_input",
        condition="True",
        routes={"true": "fork", "false": "fork"},
        fork_to=["agg_branch", "direct_branch"],
    )
    coalesce = CoalesceSettings(
        name="merge_paths",
        branches={"agg_branch": "agg_ready", "direct_branch": "direct_branch"},
        policy="quorum",
        quorum_count=2,
        timeout_seconds=1,
        merge="nested",
        on_success="output",
    )
    agg_settings = AggregationSettings(
        name="agg_branch_hold",
        plugin=batch_transform.name,
        input="agg_branch",
        on_success="agg_ready",
        on_error="discard",
        trigger=TriggerConfig(count=100, timeout_seconds=3600),
        output_mode="transform",
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(source)},
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="fork_input", options={})},
        transforms=[],
        sinks={"output": as_sink(output_sink)},
        aggregations={"agg_branch_hold": (as_transform(batch_transform), agg_settings)},
        gates=[fork_gate],
        coalesce_settings=[coalesce],
    )
    agg_node_id = graph.get_aggregation_id_map()[AggregationName("agg_branch_hold")]
    batch_transform.node_id = agg_node_id
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(batch_transform)],
        sinks={"output": as_sink(output_sink)},
        aggregation_settings={agg_node_id: agg_settings},
        gates=[fork_gate],
        coalesce_settings=[coalesce],
    )
    settings = ElspethSettings(
        sources={"primary": {"plugin": source.name, "on_success": "fork_input", "options": {}}},
        sinks={"output": {"plugin": "test", "on_write_failure": "discard"}},
        gates=[fork_gate],
        coalesce=[coalesce],
    )
    return config, graph, settings


def test_quorum_timeout_coalesce_branch_hold_is_rejected_by_rule_6() -> None:
    """Reclassified under ruling 25 (spec §7 rule 6, Task 9, WS2 controller ruling 2026-08-23).

    ``_build_quorum_timeout_coalesce``'s own docstring documents the shape
    this used to build: "the agg branch holds its token (count=100 trigger
    never fires), so only the direct branch arrives -> the barrier times
    out as quorum_not_met." That is an in-region aggregation regardless of
    output_mode, which `validate_no_aggregations_in_regions` (core/dag/
    bound_regions.py) now rejects at build time.

    Ruling 25 bans aggregators inside every bound region because it closes
    the BATCH_CONSUMED loss-blindness gap — a lost or failed batch member
    is invisible to the enclosing region's roster. The shape this builder
    demonstrated (a branch member that will NEVER arrive, resolved only by
    the barrier's own timeout) is superseded for bound regions; the engine
    behavior it used to prove END-TO-END (a durably-pending best_effort/
    quorum barrier resolving via `CoalesceExecutor.check_timeouts`, and
    ADR-019's cumulative audit-derived `rows_coalesce_failed` reconstruction
    across a pre-interrupt/resume boundary) is rebuilt at the engine level in
    `test_resume_derives_rows_coalesce_failed_from_durable_audit` below,
    which hand-builds the durable FAILED node_states this rule-6 rejection
    would otherwise have required a live run to produce (WS4 restoration
    tracked as elspeth ticket TBD, sibling of elspeth-c648d4f832).
    """
    with pytest.raises(GraphValidationError, match=r"Aggregation .* inside bound region"):
        _build_quorum_timeout_coalesce(MockClock(start=1000.0), [{"value": 10}, {"value": 20}, {"value": 30}])


def test_resume_derives_rows_coalesce_failed_from_durable_audit() -> None:
    """ADR-019 (elspeth-7294de558e): ``rows_coalesce_failed`` is reconstructed
    CUMULATIVELY from durable FAILED node_states at the run's coalesce
    nodes — run-1 (pre-interrupt) failures AND resume-time failures both
    count, from ONE audit query over the whole run_id. The old resume-only
    graft carried only re-drive failures and under-reported 1 vs 3 in the
    live-run topology this test used to build end-to-end (see
    ``test_quorum_timeout_coalesce_branch_hold_is_rejected_by_rule_6``,
    above, for why that topology is no longer buildable under ruling 25).

    Engine-level rebuild (maintainer ruling 2026-08-23, WS2 controller):
    ``count_failed_coalesce_barrier_rows`` counts DISTINCT (coalesce node,
    row_id) pairs over FAILED node_states — a PURE function of durable
    state (see its own docstring, core/landscape/run_status_projection.py)
    with no concept of "before/after resume" at all; `derive_terminal_status_from_audit`
    (aliased `derive_resume_terminal_status_from_audit`) is likewise "pure
    functions... operating on external state passed via parameters" (module
    docstring, engine/orchestrator/run_status.py) — real, unmocked
    production code, called directly against hand-built durable state,
    exactly as ``tests/unit/core/landscape/test_query_methods.py::
    TestAuditRunStatusProjection`` already does for the narrower counter
    beneath it (that suite pins the counting rules directly; THIS test
    pins the cumulative reconstruction claim ADR-019 is about). Two
    batches of FAILED node_states are written under ONE run_id — named
    "pre-interrupt" and "resume" to preserve the original scenario's
    narrative — proving the derive sums BOTH (3 total), not just the
    resume batch's own 1 (the exact shape of the bug this ADR fixed).
    """
    setup = make_recorder_with_run(run_id="run-adr019", source_node_id="source-0", source_plugin_name="json")
    factory = setup.factory
    register_test_node(factory.data_flow, "run-adr019", "coalesce-merge_paths", node_type=NodeType.COALESCE, plugin_name="coalesce")

    def _fail_barrier(row_id: str, token_id: str, state_id: str, sequence: int) -> None:
        factory.data_flow.create_row(
            "run-adr019",
            "source-0",
            0,
            {"value": 1},
            row_id=row_id,
            source_row_index=sequence,
            ingest_sequence=sequence,
        )
        factory.data_flow.create_token(row_id, token_id=token_id)
        factory.execution.begin_node_state(token_id, "coalesce-merge_paths", "run-adr019", 0, {"value": 1}, state_id=state_id)
        factory.execution.complete_node_state(
            state_id=state_id,
            status=NodeStateStatus.FAILED,
            error=CoalesceFailureReason(
                failure_reason="quorum_not_met_at_timeout",
                expected_branches=("direct_branch", "agg_branch"),
                branches_arrived=("direct_branch",),
                merge_policy="best_effort",
            ),
            duration_ms=0.0,
        )

    # "Pre-interrupt" batch: two barriers already failed and durably
    # recorded before a (simulated) shutdown — the old resume-only graft
    # would have forgotten these entirely.
    _fail_barrier("row-1", "tok-r1", "cs-r1", sequence=0)
    _fail_barrier("row-2", "tok-r2", "cs-r2", sequence=1)
    # "Resume" batch: one more barrier fails during the resumed re-drive.
    _fail_barrier("row-3", "tok-r3", "cs-r3", sequence=2)

    status, counters = derive_resume_terminal_status_from_audit(factory, "run-adr019")

    assert counters.rows_coalesce_failed == 3, (
        f"PIN (elspeth-7294de558e): the derive must reconstruct ALL 3 barrier failures cumulatively from "
        f"durable audit (2 pre-interrupt + 1 resume), not just the resume batch's own 1 — "
        f"got rows_coalesce_failed={counters.rows_coalesce_failed}, status={status}"
    )
